/* 舞台前端 v3 —— 三窗布局 + 数值 HUD + 气泡台词 + 立绘状态机
   ?demo=1 假内容预览 ｜ ?noframes=1 隐藏取景框 ｜ ?localaudio=1 页面自己出声

   v3 改了什么（对标拆解后）：
     1. 顶部 HUD 换成「余额 token / 约能聊 X 分钟」——观众买的是她的时间
     2. 底部加 31% 标题卡——手机上唯一能读清的东西
     3. 弹幕从左侧列表改成「白底气泡 + 逐字回答」——一眼看出谁在问、她在答
     4. 倒计时环：摸鱼时是"还有几秒被抓"，被抓就飘一行 −200
*/

const $ = (s) => document.querySelector(s);
const params = new URLSearchParams(location.search);
if (params.get('demo') === '1') document.body.classList.add('demo');
if (params.get('noframes') === '1') document.body.classList.add('noframes');
/* 版式：默认 v4（代码优先）；?v=stage 回到 v3 的插画舞台 */
if (params.get('v') === 'stage') document.body.classList.add('v3stage');
/* ?v=full：真实窗口铺满整屏（不套屏幕卡） */
if (params.get('v') === 'full') document.body.classList.add('vfull');
/* ?v=card：v4 的屏幕卡版式 */
if (params.get('v') === 'card') document.body.classList.add('vcard');
const LOCAL_AUDIO = params.get('localaudio') === '1';

/* ---------- 背景：合成画面 ----------
   原来是 <img src=".../bg.mjpeg">（multipart/x-mixed-replace）。
   实测 Chromium / WebView2 里这个 <img> **根本不渲染**：
   naturalWidth 恒为 0，页面看起来就像没有背景。
   （服务端那个 multipart 也没发 Transfer-Encoding: chunked，HTTP/1.1 下本来就不合规。）

   改成轮询单帧 /bg.jpg：双缓冲（先 new Image 加载完再换 src），不闪。
   想回到 mjpeg：stage.html?bg=mjpeg
   ------------------------------------------------------------------ */
const elBg = document.getElementById('bg');
const BG_URL = 'http://127.0.0.1:8791/bg.jpg';
const BG_MS = 150;                 // ≈6.7 帧/秒，屏幕共享够用
let bgBusy = false;

function startBgPolling() {
  if (!elBg) return;
  elBg.removeAttribute('src');     // 先掐掉 mjpeg 的加载
  elBg.style.display = 'block';
  const tick = () => {
    if (bgBusy) return;
    bgBusy = true;
    const im = new Image();
    im.onload = () => { elBg.src = im.src; bgBusy = false; };
    im.onerror = () => { bgBusy = false; };
    im.src = BG_URL + '?t=' + Date.now();
  };
  setInterval(tick, BG_MS);
  tick();
}
if (params.get('bg') !== 'mjpeg') startBgPolling();

/* ---------- 缩放 ---------- */
function fit() {
  const s = Math.min(window.innerWidth / 1080, window.innerHeight / 1920);
  document.documentElement.style.setProperty('--s', s.toFixed(4));
}
window.addEventListener('resize', fit);
fit();

/* ---------- 窗口开关状态 ---------- */
const missEls = {
  code: document.getElementById('miss-code'),
  web: document.getElementById('miss-web'),
  wechat: document.getElementById('miss-wechat'),
};

async function syncSlots() {
  try {
    const r = await fetch('/api/windows', { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    for (const it of (d.windows || [])) {
      const el = missEls[it.key];
      if (el) el.classList.toggle('on', !it.rect);
    }
  } catch (e) { /* 服务没起就算了 */ }
}
setInterval(syncSlots, 1500);
syncSlots();

/* ---------- 元素 ---------- */
const elTitle = $('#title');
const elAvatarWrap = $('#avatarWrap');
const elAvatar = $('#avatar');
const elDanmaku = $('#danmaku');

const elHud = $('#hud');
const elBalance = $('#hudBalance');
const elMinutes = $('#hudMinutes');
const elBar = $('#hudBar');
const elFocus = $('#hudFocus');
const elSlack = $('#hudSlack');
const elCaught = $('#hudCaught');
const elStatus = $('#hudStatus');
const elSpeed = $('#hudSpeed');
const elRule = $('#hudRule');
const elRuleTag = $('#hudRuleTag');
const elClock = $('#hudClock');

const elCd = $('#hudCd');
const elRing = $('#ring');
const elRingNum = $('#ringNum');
const elRingFg = $('#ringFg');
const elPulse = $('#pulse');

const elTalk = $('#talk');
const elBubble = $('#bubble');
const elBubbleName = $('#bubbleName');
const elBubbleText = $('#bubbleText');
const elReply = $('#reply');
const elReplyText = $('#replyText');

const elKpiBar = $('#kpiBar');
const elKpiNum = $('#kpiNum');
const elGuide = $('#guide');
const elBlackout = $('#blackout');
const elBkLeft = $('#bkLeft');
const elGPos  = $('#gPos');
// 今日 KPI：到点就是「提前下班」，这是给观众看的赌注
const KPI_TARGET = 3000;

/* ============================================================
   立绘差分
   ============================================================ */
/* ?v= 是缓存穿透：WebView2 会把同名 webp 缓存住，换图后不重新拉。
   改立绘后把这里和 stage.html 的 img src 一起 +1。 */
const AV = '?v=6';
const FRAMES = {
  base: 'avatar/sit/base.webp' + AV,
  blink: 'avatar/sit/blink.webp' + AV,
  talk: 'avatar/sit/talk.webp' + AV,
  sing: 'avatar/sit/sing.webp' + AV,
  happy: 'avatar/sit/happy.webp' + AV,
  panic: 'avatar/sit/panic.webp' + AV,
  // v3 新增（生成好就自动生效，文件不在也不报错）
  smug: 'avatar/sit/smug.webp' + AV,      // 摸鱼：墨镜得意
  bored: 'avatar/sit/bored.webp' + AV,    // 托腮发呆
  wow: 'avatar/sit/wow.webp' + AV,        // 惊讶
  sad: 'avatar/sit/sad.webp' + AV,        // 委屈
  away: 'avatar/sit/away.webp' + AV,      // 侧身背对
  sleep: 'avatar/sit/sleep.webp' + AV,    // 趴桌睡
};
Object.values(FRAMES).forEach((u) => { const i = new Image(); i.src = u; });

let mode = 'idle';        // idle | talking | singing
let liveStatus = 'focus'; // focus | slack | caught | idle | chat
let blinkTimer = null;
let idleActTimer = null;
let flapTimer = null;
let frameOk = {};         // 哪些差分真的加载成功了

Object.entries(FRAMES).forEach(([k, u]) => {
  const i = new Image();
  i.onload = () => { frameOk[k] = true; };
  i.onerror = () => { frameOk[k] = false; };
  i.src = u;
});

function has(key) { return frameOk[key] !== false; }

function showFrame(key) {
  if (!has(key)) key = 'base';
  const src = FRAMES[key] || FRAMES.base;
  if (elAvatar.getAttribute('src') !== src) elAvatar.setAttribute('src', src);
}

/* 状态 → 姿态。没有对应差分就退回 base，绝不显示裂图 */
let idleFlip = false;
function poseForStatus(st) {
  if (st === 'blackout') return has('sleep') ? 'sleep' : 'base';
  if (st === 'caught') return has('panic') ? 'panic' : 'base';
  // 摸鱼 / 聊天 = 抬头（talk）；专注 = 低头敲键盘（base）。
  // 这两张必须能一眼分开，否则观众看不出她什么时候在偷懒。
  if (st === 'slack' || st === 'chat') return has('talk') ? 'talk' : (has('smug') ? 'smug' : 'base');
  if (st === 'idle') {
    // 人不在的时候两种姿态轮着来：托腮发呆 / 背过身生气
    idleFlip = !idleFlip;
    if (idleFlip && has('away')) return 'away';
    return has('bored') ? 'bored' : 'base';
  }
  return 'base';
}

/* 临时切一帧再切回来（收到礼物"哇"一下、被夸"得意"一下） */
let flashTimer = null;
function flashFrame(key, ms = 1300) {
  if (!has(key)) return;
  clearTimeout(flashTimer);
  showFrame(key);
  flashTimer = setTimeout(() => {
    if (mode === 'idle') showFrame(poseForStatus(liveStatus));
  }, ms);
}

/* ---------- 眨眼 ---------- */
function scheduleBlink() {
  clearTimeout(blinkTimer);
  if (mode !== 'idle') return;
  const wait = 2400 + Math.random() * 4200;
  blinkTimer = setTimeout(() => {
    if (mode !== 'idle') return;
    if (!has('blink')) { scheduleBlink(); return; }
    showFrame('blink');
    setTimeout(() => {
      if (mode === 'idle') { showFrame(poseForStatus(liveStatus)); scheduleBlink(); }
    }, 110 + Math.random() * 70);
  }, wait);
}

/* ---------- 日常小动作 ---------- */
function scheduleIdleAct() {
  clearTimeout(idleActTimer);
  const wait = 7000 + Math.random() * 11000;
  idleActTimer = setTimeout(() => {
    if (mode !== 'idle') { scheduleIdleAct(); return; }
    if (liveStatus === 'slack' || liveStatus === 'caught' || liveStatus === 'idle') {
      scheduleIdleAct(); return;
    }
    const pick = Math.random() < 0.55 ? 'wave' : 'happy';
    showFrame(pick);
    elAvatarWrap.classList.add(pick === 'wave' ? 'acting-wave' : 'acting-happy');
    const hold = 900 + Math.random() * 800;
    setTimeout(() => {
      if (mode === 'idle') {
        showFrame(poseForStatus(liveStatus));
        elAvatarWrap.classList.remove('acting-wave', 'acting-happy');
      }
      scheduleIdleAct();
    }, hold);
  }, wait);
}

/* ---------- 说话 ---------- */
function startTalking() {
  if (mode === 'talking') return;
  mode = 'talking';
  clearTimeout(blinkTimer);
  clearTimeout(idleActTimer);
  elAvatarWrap.classList.remove('singing', 'acting-wave', 'acting-happy');
  elAvatarWrap.classList.add('talking', 'working');
  clearInterval(flapTimer);
  flapTimer = setInterval(() => {
    if (mode !== 'talking') return;
    showFrame(Math.random() < 0.55 ? 'talk' : 'base');
  }, 120 + Math.random() * 50);
}

/* ---------- 唱歌 ---------- */
function startSinging() {
  mode = 'singing';
  clearTimeout(blinkTimer);
  clearTimeout(idleActTimer);
  clearInterval(flapTimer);
  elAvatarWrap.classList.remove('talking', 'acting-wave', 'acting-happy');
  elAvatarWrap.classList.add('singing', 'working');
  showFrame('sing');
}

/* ---------- 回到待机 ---------- */
function goIdle() {
  mode = 'idle';
  clearInterval(flapTimer);
  elAvatarWrap.classList.remove('talking', 'singing', 'acting-wave', 'acting-happy');
  showFrame(poseForStatus(liveStatus));
  elAvatarWrap.classList.add('working');
  scheduleBlink();
  scheduleIdleAct();
}

/* ============================================================
   台词区：气泡 + 逐字
   ============================================================ */
let typeTimer = null;
let replyHideTimer = null;

function showBubble(uname, text) {
  if (!text) return;
  elBubbleName.textContent = uname ? ('@' + uname) : '@水友';
  elBubbleText.textContent = text;
  elBubble.classList.add('show');
  flashGuide();     // 有人开口 → 顺手把「这是什么」再敲一遍
}

function typeReply(text, holdMs, singing) {
  if (!text) return;
  clearInterval(typeTimer);
  clearTimeout(replyHideTimer);

  const chars = Array.from(text);
  let i = 0;
  elReplyText.textContent = '';
  elReply.classList.add('show', 'typing');
  elTalk.classList.add('show');
  elReply.classList.toggle('singing', !!singing);

  // 46px 字号下，一屏大约 20 字；按 42ms/字打，跟语音差不多同步
  const step = Math.max(26, Math.min(60, (holdMs || 3000) / Math.max(1, chars.length) * 0.72));
  typeTimer = setInterval(() => {
    if (i >= chars.length) {
      clearInterval(typeTimer);
      elReply.classList.remove('typing');
      return;
    }
    elReplyText.textContent += chars[i++];
  }, step);

  singing ? startSinging() : startTalking();

  const total = Math.max(1600, holdMs || 0);
  replyHideTimer = setTimeout(() => {
    goIdle();
    replyHideTimer = setTimeout(() => {
      elReply.classList.remove('show');
      elBubble.classList.remove('show');
      elTalk.classList.remove('show');
    }, 2200);
  }, total);
}

/* 旧的 showCaption 名字保留，避免其它脚本调用时炸掉 */
function showCaption(text, holdMs, singing) { typeReply(text, holdMs, singing); }

/* ---------- 语音 ---------- */
let currentAudio = null;
let lastAudioUrl = '';

function playAudio(url) {
  if (!url || url === lastAudioUrl) return;
  lastAudioUrl = url;
  try {
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    const a = new Audio(url);
    currentAudio = a;
    a.addEventListener('ended', () => {
      goIdle();
      clearTimeout(replyHideTimer);
      replyHideTimer = setTimeout(() => {
        elReply.classList.remove('show');
        elBubble.classList.remove('show');
        elTalk.classList.remove('show');
      }, 2200);
    });
    a.play().catch(() => {});
  } catch (e) { /* 忽略 */ }
}

/* ============================================================
   HUD：余额 / 状态 / 手速 / 守则 / 倒计时
   ============================================================ */
const ST_CLASS = { focus: 'st-focus', slack: 'st-slack', caught: 'st-caught', idle: 'st-idle', chat: 'st-chat' };
let lastPulseTs = 0;
let lastStatus = '';

function pad2(n) { return String(n).padStart(2, '0'); }

function renderClock() {
  const d = new Date();
  const wd = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()];
  elClock.textContent = `🕐 ${wd} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}
setInterval(renderClock, 1000);
renderClock();

function renderLive(lv, work) {
  // 后端还没升级（没有 live 字段）时退回旧的 work 面板，别把 HUD 空在那儿
  if ((!lv || typeof lv !== 'object') && work && typeof work === 'object') {
    lv = {
      status: work.status || 'focus',
      status_text: ({ focus: '专心工作中', slack: '摸鱼中', caught: '被抓了', idle: '人不在' })[work.status] || '专心工作中',
      rule: '赚够 token 就能合法摸鱼',
      balance: Math.round(Number(work.salary || 0) / 4),
      minutes_left: Math.round(Number(work.salary || 0) / 600),
      focus_sec: work.focus_sec || 0,
      slack_count: work.slack_count || 0,
      caught_count: 0,
      speed: work.speed || 1,
      countdown: 20,
      pulse: null,
    };
  }
  if (!lv || typeof lv !== 'object') return;

  const st = lv.status || 'focus';
  if (st !== lastStatus) {
    lastStatus = st;
    for (const c of Object.values(ST_CLASS)) elHud.classList.remove(c);
    elHud.classList.add(ST_CLASS[st] || 'st-focus');
    liveStatus = st;
    if (mode === 'idle') showFrame(poseForStatus(st));
    if (st === 'caught') {
      document.body.classList.remove('flash');
      void document.body.offsetWidth;
      document.body.classList.add('flash');
      setTimeout(() => document.body.classList.remove('flash'), 1600);
    }
  }

  elStatus.textContent = lv.status_text || '专心工作中';
  elBalance.textContent = Number(lv.balance || 0).toLocaleString();
  elMinutes.textContent = `约能聊 ${Number(lv.minutes_left || 0)} 分钟`;

  // 今日 KPI 进度：观众看得见「还差多少」，才有一起推的动力
  const bal = Number(lv.balance || 0);
  const pct = Math.max(0, Math.min(1, bal / KPI_TARGET));
  if (elKpiBar) elKpiBar.style.width = (pct * 100).toFixed(1) + '%';
  if (elKpiNum) elKpiNum.textContent = `${Math.round(bal)} / ${KPI_TARGET}`;
  if (elGuide) elGuide.classList.toggle('done', bal >= KPI_TARGET);

  // 余额扣光 → 全屏断电倒计时
  const bk = Number(lv.blackout_left || 0);
  if (elBlackout) elBlackout.classList.toggle('on', bk > 0);
  if (elBkLeft && bk > 0) elBkLeft.textContent = Math.ceil(bk);
  // 进度条：本分钟倒数（60s 走完一格）。绑余额是看不出变化的——每分钟才涨 8。
  const mp = (lv.minute_left != null) ? Number(lv.minute_left)
                                     : Math.min(1, Number(lv.balance || 0) / 3000);
  elBar.style.width = (mp * 100).toFixed(1) + '%';

  const fs = Number(lv.focus_sec || 0);
  elFocus.textContent = fs >= 60 ? Math.floor(fs / 60) : fs;
  elSlack.textContent = lv.slack_count || 0;
  elCaught.textContent = lv.caught_count || 0;
  elSpeed.textContent = '×' + Number(lv.speed || 1).toFixed(1);
  // 员工守则这一行改成**实时判决**：观众不用猜她现在到底在不在摸鱼
  const VERDICT = {
    focus:    ['员工守则', '她在干活 —— 举报她算诬告'],
    slack:    ['摸鱼须知', '她在摸鱼 —— 可以举报了'],
    chat:     ['摸鱼须知', '她在回话 —— 手已经离开键盘'],
    caught:   ['被抓记录', '被抓了 —— 扣 200 token'],
    idle:     ['离开工位', '人不在 —— 抓也没用'],
    blackout: ['断电记录', '余额扣光 —— 黑屏 30 秒'],
  };
  const vd = VERDICT[st] || ['员工守则', lv.rule || '赚够 token 就能合法摸鱼'];
  elRule.textContent = vd[1];
  elRuleTag.textContent = vd[0];

  // 倒计时环：摸鱼时 12→0 就是"还有几秒被抓"
  const cd = Number(lv.countdown || 0);
  elRingNum.textContent = cd;
  if (elCd) {
    elCd.textContent = cd + 's';
    elCd.classList.toggle('hot', st === 'slack' && cd <= 4);
  }
  const total = st === 'slack' ? 12 : 20;
  const ratio = Math.max(0, Math.min(1, cd / total));
  elRingFg.style.strokeDashoffset = (326.7 * (1 - ratio)).toFixed(1);
  elRing.classList.toggle('hot', st === 'slack' && cd <= 4);

  // 飘字
  const p = lv.pulse;
  if (p && p.ts && p.ts > lastPulseTs) {
    lastPulseTs = p.ts;
    const div = document.createElement('div');
    div.className = 'pulse-item ' + (p.kind || 'info');
    div.textContent = p.text || '';
    elPulse.appendChild(div);
    setTimeout(() => div.remove(), 1700);
    while (elPulse.children.length > 3) elPulse.removeChild(elPulse.firstChild);
    // 收到礼物"哇"一下；被抓的时候给个委屈脸
    if (p.kind === 'gain' && mode === 'idle') flashFrame('wow', 1400);
    if (p.kind === 'loss' && mode === 'idle') flashFrame('sad', 1800);
  }

  // 余额见底：趴在桌上不动了（观众能看懂"她没钱了"）
  if (Number(lv.balance || 0) <= 0 && mode === 'idle' && has('sleep')) {
    showFrame('sleep');
  }
}

/* ---------- 弹幕 ---------- */
const seen = new Set();
let lastBubbleId = '';

function renderDanmaku(list) {
  if (!Array.isArray(list)) return;
  for (const item of list) {
    if (!item || !item.id || seen.has(item.id)) continue;
    seen.add(item.id);
    pushDanmaku(item);
    // 最新一条进气泡
    if (item.id !== lastBubbleId) {
      lastBubbleId = item.id;
      showBubble(item.uname, item.text);
    }
  }
  while (elDanmaku.children.length > 3) elDanmaku.removeChild(elDanmaku.firstChild);
}

function pushDanmaku(item) {
  const div = document.createElement('div');
  div.className = 'dm';
  const b = document.createElement('b');
  b.textContent = item.uname || '水友';
  div.appendChild(b);
  div.appendChild(document.createTextNode(item.text || ''));
  elDanmaku.appendChild(div);
  setTimeout(() => {
    div.classList.add('out');
    setTimeout(() => div.remove(), 520);
  }, 22000);
}

/* ---------- 导引句：有人进房时闪一下，把「这是什么」再敲一遍 ---------- */
function flashGuide() {
  if (!elGPos) return;
  elGPos.classList.remove('flash');
  void elGPos.offsetWidth;          // 强制重排，否则连点两次动画不重播
  elGPos.classList.add('flash');
}

/* ---------- 轮询 ---------- */
let version = 0;
let offline = false;
let fails = 0;

async function poll() {
  try {
    const r = await fetch(`/api/state?since=${version}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    fails = 0;
    if (offline) {
      offline = false;
      stopLocalDemo();
    }
    if (s.changed === false) return;
    version = s.v || 0;

    renderLive(s.live, s.work);
    if (elTitle && s.title != null && s.title !== elTitle.textContent) elTitle.textContent = s.title;

    if (s.caption) {
      const holdMs = Math.max(0, (s.speaking_until || 0) * 1000 - Date.now());
      if (holdMs > 0 && s.caption !== lastCaption) {
        lastCaption = s.caption;
        typeReply(s.caption, holdMs, s.mode === 'sing');
        if (s.audio && LOCAL_AUDIO) playAudio(s.audio);
      }
    }
    renderDanmaku(s.danmaku);
  } catch (e) {
    fails++;
    if (!offline && fails >= 2) {
      offline = true;
      document.body.classList.add('offline', 'demo');
      startLocalDemo();
    }
  }
}
let lastCaption = '';

/* ---------- 无服务端时的本地演示 ---------- */
let demoTimer = null;

function stopLocalDemo() {
  if (demoTimer) { clearInterval(demoTimer); demoTimer = null; }
  document.body.classList.remove('demo', 'offline');
  elDanmaku.replaceChildren();
}

function startLocalDemo() {
  if (elTitle) elTitle.textContent = '我在写代码';
  const lines = [
    ['麟迟', '工资没了是不是休息了'],
    ['席安', '怎么不找外包来干'],
    ['十四', '要不要跑路'],
  ];
  renderLive({
    status: 'slack', status_text: '摸鱼中', rule: '摸鱼就会白干',
    balance: 1017, minutes_left: 7, salary: 2600, salary_max: 6000,
    focus_sec: 1800, slack_count: 56, caught_count: 0, speed: 5.0,
    countdown: 9, pulse: null,
  });
  showBubble('麟迟', '怎么不找外包来干，比如豆包');
  typeReply('外包？这山的 bug 只认我这双手。', 6000, false);
  let i = 0;
  if (demoTimer) clearInterval(demoTimer);
  demoTimer = setInterval(() => {
    const [u, t] = lines[i % lines.length];
    pushDanmaku({ uname: u, text: t, id: 'demo-' + i++ });
    showBubble(u, t);
    while (elDanmaku.children.length > 3) elDanmaku.removeChild(elDanmaku.firstChild);
  }, 5200);
}

scheduleBlink();
scheduleIdleAct();
setInterval(poll, 500);
poll();

window.stageSay = (t) => typeReply(t, 4000, false);
window.stageSing = (t) => typeReply(t, 8000, true);
