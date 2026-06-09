#!/usr/bin/env node
/**
 * 推特监控 → A股下单（iQuant 文件单 / miniQMT）
 *
 * 链路：轮询 kelvv 自己的推特 timeline → 每条新推交 Grok 判「是否实质讨论某只沪深京 A 股」
 *       → 命中 → 提取 6 位代码 → 风控（金额/次数）→ 调下单器写文件单 txt → 平台下单。
 *
 * 判定逻辑直接抄自成熟的 aleabit-astock-watch（白毛女监控）：
 *   - Grok 判定门：必须实质讨论某只 A 股才算（荐股/点评/复盘/列清单/对比标的），
 *     拿公司名做类比、只提产品赛道作背景板、纯顺嘴一提一律 false。
 *   - 确定性展开 t.co 短链：把 `300376.SZ` 这类被 X 转短链的伪域名还原成明文。
 *   - A 股字典兜底：Grok 判 false 时硬扫文本里收录的 6 位代码，捞回漏判。
 *
 * 下单两条路（按 .env 的 ORDER_MODE 选）：
 *   - file   ：写文件单 txt（国信官方外部下单，需管理端开权限 + 客户端启动文件单策略）
 *   - miniqmt：调 trader.py（需 miniQMT 交易接口，connect 当前 -1，权限没开）
 *   - dry    ：只判定+通知，不真下单（默认，先跑通监控）
 *
 * 用法：
 *   node twitter_watch.js              # 常驻（PM2）
 *   node twitter_watch.js --test "推文文本"   # 单条判定，不下单不通知
 *
 * 环境变量（.env）：
 *   X_BEARER_TOKEN  X_WATCH_USER  X_POLL_INTERVAL  XAI_API_KEY  GROK_MODEL
 *   ORDER_MODE(file|miniqmt|dry)  ORDER_AMOUNT  FILE_ORDER_PATH  PY311  QMT_*
 */

const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');

// ---- 极简 .env 加载（不依赖 dotenv）----
(function loadEnv() {
  const p = path.join(__dirname, '..', '.env');
  if (!fs.existsSync(p)) return;
  for (const line of fs.readFileSync(p, 'utf8').replace(/^﻿/, '').split('\n')) {
    const s = line.trim();
    if (!s || s.startsWith('#') || !s.includes('=')) continue;
    const i = s.indexOf('=');
    const k = s.slice(0, i).trim(), v = s.slice(i + 1).trim();
    if (!(k in process.env)) process.env[k] = v;
  }
})();

const X_BEARER = process.env.X_BEARER_TOKEN || '';
const WATCH_USER = process.env.X_WATCH_USER || '';
const POLL_MS = (parseInt(process.env.X_POLL_INTERVAL || '60', 10)) * 1000;
const XAI_KEY = process.env.XAI_API_KEY || '';
const XAI_URL = 'https://api.x.ai/v1/responses';
const XAI_MODEL = process.env.GROK_MODEL || 'grok-4-1-fast-reasoning';
const ORDER_MODE = (process.env.ORDER_MODE || 'dry').toLowerCase(); // file|miniqmt|dry
const ORDER_AMOUNT = parseFloat(process.env.ORDER_AMOUNT || '5000'); // 每单固定金额(元)
const MAX_PER_STOCK = parseInt(process.env.MAX_ORDERS_PER_STOCK || '3', 10);
const PY311 = process.env.PY311 || 'C:\\Py311\\python.exe';
const FILE_ORDER_PATH = process.env.FILE_ORDER_PATH || '';
const PROXY = process.env.HTTP_PROXY || '';

const TEST = process.argv.includes('--test') ? process.argv[process.argv.indexOf('--test') + 1] : null;

const seen = new Set();        // 推文去重
const orderCount = {};         // 代码 -> 今日下单次数（进程内，重启清零）
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ---- A 股字典兜底（复用 aleabit 的 ashare-dict）----
let DICT = { codes: {} };
try {
  DICT = JSON.parse(fs.readFileSync(path.join(__dirname, 'ashare-dict.json'), 'utf8'));
  console.log(`[dict] 载入 ${Object.keys(DICT.codes).length} 只 A 股代码`);
} catch { console.log('[dict] 无字典，兜底禁用（不影响 Grok 主判）'); }

function dictScan(text) {
  const hits = new Map();
  for (const code of (String(text || '').match(/\d{6}/g) || [])) {
    if (DICT.codes && DICT.codes[code]) hits.set(code, DICT.codes[code]);
  }
  return [...hits].map(([c, n]) => `${n}(${c})`);
}

// ---- 确定性展开 t.co 短链（把伪域名 300376.sz 还原成明文）----
async function expandShortLinks(text) {
  const links = [...new Set((text.match(/https?:\/\/t\.co\/[A-Za-z0-9]+/g) || []))];
  let out = text;
  for (const link of links) {
    try {
      const r = await fetch(link, { method: 'HEAD', redirect: 'manual' });
      const dest = r.headers.get('location') || '';
      const m = dest.match(/(?:https?:\/\/)?(\d{6})\.(sz|sh|bj)\b/i);
      if (m) out = out.split(link).join(`${link} [A股代码 ${m[1]}.${m[2].toUpperCase()}]`);
    } catch { /* 单条失败不影响 */ }
  }
  return out;
}

// ---- Grok 判定（抄 aleabit 判定门）----
async function judge(rawText) {
  if (!XAI_KEY) throw new Error('XAI_API_KEY 未配置');
  const text = await expandShortLinks(rawText);
  const sys = '你是 A 股识别助手，熟悉中国沪深京 A 股全部股票名称与代码。只输出 JSON，不要解释。';
  const user = `判断下面这条推文**是否在实质讨论某一只中国沪深京 A 股股票**。要明确把某只 A 股当作讨论对象才算命中，不能靠推测。
【命中(true)】把这只 A 股当成讨论对象：推荐、点评、吐槽、复盘、列入清单、或作为对比/估值标的。
【不命中(false)】只顺带出现公司名、并非讨论这只股：拿公司名做风格类比、只提产品/业务/赛道作背景板、纯顺嘴一提、泛泛谈大盘板块。宁可判 false。
正文里 \`[A股代码 NNNNNN.SZ]\` 是作者原文写的代码（已展开），作者主动打出 6 位代码=明确点名=命中。
范围严格限沪深京 A 股(6/0/3/8/4 开头 6 位)，港股/美股/中概/加密一律不算。
只回 JSON：{"mentions_ashare":true/false,"stocks":"命中A股,多只用、隔开,如 中恒电气(300376,创业板)","reason":"一句话说明"}。
推文："""${text}"""`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 20000);
  try {
    const r = await fetch(XAI_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${XAI_KEY}` },
      body: JSON.stringify({ model: XAI_MODEL, tools: [], input: [
        { role: 'system', content: sys }, { role: 'user', content: user },
      ] }),
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error(`grok ${r.status}`);
    const j = await r.json();
    let reply = '';
    for (const item of (j.output || [])) {
      if (item.type === 'message' && item.content) {
        for (const b of item.content) if (b.type === 'output_text' || b.type === 'text') reply += b.text;
      }
    }
    const m = reply.match(/\{[\s\S]*\}/);
    if (!m) throw new Error('Grok 无 JSON: ' + reply.slice(0, 100));
    const parsed = JSON.parse(m[0]);
    parsed._text = text;
    return parsed;
  } finally { clearTimeout(timer); }
}

function extractCodes(stocks) {
  return [...new Set((String(stocks || '').match(/\d{6}/g) || []))];
}

// ---- 下单（按 ORDER_MODE 分发）----
function pyRun(script, args) {
  return new Promise((resolve) => {
    execFile(PY311, [path.join(__dirname, '..', 'trader', script), ...args],
      { timeout: 30000 }, (err, stdout, stderr) => {
        if (err) console.error(`[order-err] ${script}`, stderr || err.message);
        else console.log(`[order-out] ${stdout.trim()}`);
        resolve(!err);
      });
  });
}

async function placeOrder(code, reason) {
  // 风控：单只票次数上限（金额上限在下单器内，这里固定金额买）
  const cnt = orderCount[code] || 0;
  if (cnt >= MAX_PER_STOCK) {
    console.log(`[risk] ${code} 今日已下 ${cnt} 单 >= ${MAX_PER_STOCK}，跳过`);
    return;
  }
  // 固定金额 → 股数：需当前价才能算手数。先以「最新价 + 固定金额」交给下单器/面板处理。
  // 文件单的报价方式由面板"最新价"决定，数量这里按固定金额粗算（实盘前需接行情拿现价精算）。
  // TODO: 接 xtdata.get_full_tick 拿现价 → floor(金额/价/100)*100 算精确手数。暂用占位 100 股。
  const volume = 100; // 占位：1 手。接入行情后改为 ORDER_AMOUNT 换算
  console.log(`[ORDER] ${ORDER_MODE} 买 ${code} ${volume}股 (固定金额${ORDER_AMOUNT}元) 因:${reason}`);

  let ok = false;
  if (ORDER_MODE === 'file') {
    if (!FILE_ORDER_PATH) { console.error('[order-err] FILE_ORDER_PATH 未配'); return; }
    ok = await pyRun('file_order.py', ['buy', code, String(volume), '--path', FILE_ORDER_PATH]);
  } else if (ORDER_MODE === 'miniqmt') {
    ok = await pyRun('trader.py', ['buy', code, String(volume)]);
  } else {
    console.log('[dry] 演练模式，不真下单');
    ok = true;
  }
  if (ok) orderCount[code] = cnt + 1;
}

// ---- 处理一条推文 ----
async function handle(text, { order = true } = {}) {
  let v;
  try { v = await judge(text); }
  catch (e) { console.error('[grok-err]', e.message); return; }
  let hit = !!v.mentions_ashare;
  let stocks = v.stocks || '';

  // 字典兜底
  if (!hit) {
    const dh = dictScan(v._text || text);
    if (dh.length) { hit = true; stocks = dh.join('、'); console.log('[dict-catch]', stocks); }
  }
  console.log(`[grok] hit=${hit} stocks=${stocks} ${v.reason || ''}`);
  if (!hit || !order) return hit;

  // 命中 → 对每只票下单
  for (const code of extractCodes(stocks)) {
    await placeOrder(code, v.reason || stocks);
  }
  return hit;
}

// ---- 拉自己的 timeline ----
let userId = null;
async function resolveUserId() {
  const r = await fetch(`https://api.twitter.com/2/users/by/username/${WATCH_USER}`,
    { headers: { Authorization: 'Bearer ' + X_BEARER } });
  if (!r.ok) throw new Error(`resolve user ${r.status}`);
  userId = (await r.json()).data.id;
  console.log(`[x] @${WATCH_USER} -> id=${userId}`);
}

let sinceId = null;
async function poll() {
  try {
    const url = new URL(`https://api.twitter.com/2/users/${userId}/tweets`);
    url.searchParams.set('max_results', '10');
    url.searchParams.set('tweet.fields', 'note_tweet,text,created_at');
    if (sinceId) url.searchParams.set('since_id', sinceId);
    const r = await fetch(url, { headers: { Authorization: 'Bearer ' + X_BEARER } });
    if (!r.ok) { console.error('[x-poll]', r.status); return; }
    const j = await r.json();
    const tweets = (j.data || []).reverse(); // 旧→新
    for (const t of tweets) {
      if (seen.has(t.id)) continue;
      seen.add(t.id);
      sinceId = t.id;
      const full = (t.note_tweet && t.note_tweet.text) || t.text || '';
      console.log(`[tw] ${t.id} ${full.slice(0, 50)}...`);
      await handle(full).catch(e => console.error('[handle-err]', e.message));
    }
  } catch (e) { console.error('[poll-err]', e.message); }
}

async function main() {
  if (TEST !== null) {
    console.log('=== TEST（不下单不通知）===\n文本:', TEST);
    const hit = await handle(TEST, { order: false });
    console.log(hit ? '✅ 判定命中 A 股' : '⚪ 未命中');
    return;
  }
  if (!WATCH_USER || !X_BEARER) { console.error('[fatal] X_WATCH_USER / X_BEARER_TOKEN 未配'); process.exit(1); }
  console.log(`[start] 监控 @${WATCH_USER} 每 ${POLL_MS / 1000}s 一轮，下单模式=${ORDER_MODE}`);
  await resolveUserId();
  await poll();
  setInterval(poll, POLL_MS);
}

main().catch(e => { console.error('[fatal]', e); process.exit(1); });
