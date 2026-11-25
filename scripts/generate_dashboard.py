#!/usr/bin/env python3
"""把含 JD 的主表 JSON 生成一个单文件、离线可用的可视化看板 HTML。"""
import argparse
import collections
import json
import os
import re
import statistics

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__ · 岗位数据看板</title>
<style>
:root{
  --bg:#080c16; --panel:#111827; --panel2:#0d1424; --line:#1f2b45;
  --text:#e8eefc; --muted:#8fa3c8; --accent:#4f7cff; --accent2:#7c5cff;
  --cyan:#22d3ee; --pink:#f472b6; --amber:#fbbf24; --green:#34d399;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:14px;line-height:1.6}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1440px;margin:0 auto;padding:28px 24px 80px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px;
  padding-bottom:22px;border-bottom:1px solid var(--line);margin-bottom:24px}
h1{margin:0;font-size:28px;letter-spacing:.5px;font-weight:700}
h1 span{background:linear-gradient(90deg,var(--accent),var(--cyan));-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--muted);font-size:13px;margin-top:6px}
.tag{display:inline-block;padding:3px 10px;border:1px solid var(--line);border-radius:999px;
  color:var(--muted);font-size:12px;margin-left:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:24px}
.card{background:linear-gradient(145deg,var(--panel),var(--panel2));border:1px solid var(--line);
  border-radius:14px;padding:18px 20px}
.card .k{color:var(--muted);font-size:12px;letter-spacing:.5px}
.card .v{font-size:30px;font-weight:700;margin-top:6px;line-height:1.1}
.card .v small{font-size:14px;color:var(--muted);font-weight:400}
.card.c1 .v{color:var(--accent)} .card.c2 .v{color:var(--cyan)}
.card.c3 .v{color:var(--amber)} .card.c4 .v{color:var(--green)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px;margin-bottom:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}
.panel h2{margin:0 0 16px;font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px}
.panel h2::before{content:"";width:4px;height:15px;border-radius:2px;background:linear-gradient(var(--accent),var(--accent2))}
.bars{display:flex;flex-direction:column;gap:9px}
.bar{display:grid;grid-template-columns:minmax(110px,150px) 1fr 46px;align-items:center;gap:10px;font-size:12px}
.bar .lbl{color:var(--muted);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .track{height:9px;background:#0a1020;border-radius:6px;overflow:hidden}
.bar .fill{height:100%;border-radius:6px;background:linear-gradient(90deg,var(--accent),var(--cyan));min-width:2px}
.bar .num{text-align:right;color:var(--text);font-variant-numeric:tabular-nums}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{padding:5px 11px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);font-size:12px}
.chip b{color:var(--cyan);margin-left:5px;font-variant-numeric:tabular-nums}
.chip.hot{border-color:#2c4a8a;background:#132042}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px;cursor:pointer;user-select:none;white-space:nowrap;position:sticky;top:0;background:var(--panel);z-index:2}
th:hover{color:var(--accent)}
tr.row{cursor:pointer}
tr.row:hover td{background:#151f36}
.sal{color:var(--amber);font-weight:600;white-space:nowrap}
.co{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{display:inline-block;padding:2px 8px;border-radius:6px;background:#18233d;color:var(--muted);font-size:11px;margin:1px 3px 1px 0;white-space:nowrap}
.pill.hi{background:#17304a;color:var(--cyan)}
.active{color:var(--green)}
.jdrow td{background:#0b1222;color:#b8c8e6;padding:14px 18px;white-space:pre-wrap;font-size:12px;line-height:1.8;max-height:340px;overflow:auto}
.controls{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px}
input,select{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:8px;
  padding:9px 12px;font-size:13px;outline:none;font-family:inherit}
input:focus,select:focus{border-color:var(--accent)}
input[type=search]{min-width:260px}
.count{color:var(--muted);font-size:12px;margin-left:auto;align-self:center}
.tblwrap{max-height:760px;overflow:auto;border:1px solid var(--line);border-radius:12px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:32px}
.empty{text-align:center;color:var(--muted);padding:30px}
/* 工作台：登录引导 + 对话面板 */
.loginbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:14px 18px;margin-bottom:18px;
  border:1px solid var(--line);border-radius:12px;background:linear-gradient(100deg,#101c34,#0c1424)}
.dot{width:9px;height:9px;border-radius:50%;background:#f59e0b;box-shadow:0 0 10px #f59e0b66;flex:none}
.dot.on{background:var(--green);box-shadow:0 0 10px #34d39966}
.dot.off{background:#ef4444;box-shadow:0 0 10px #ef444466}
.loginbar .msg{color:var(--muted);font-size:13px;flex:1;min-width:180px}
.resumebar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:14px 18px;margin-bottom:18px;
  border:1px solid #24406b;border-radius:12px;background:linear-gradient(100deg,#101f38,#0c1424)}
.resumebar .ricon{width:40px;height:40px;border-radius:10px;background:#17304a;color:var(--cyan);
  display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex:none;letter-spacing:.5px}
.resumebar .rmain{flex:1;min-width:240px}
.resumebar .rtitle{font-size:13px;font-weight:600}
.resumebar .rtitle span{color:var(--muted);font-weight:400;margin-left:8px;font-size:12px}
.resumebar .rsub{color:var(--muted);font-size:12px;margin-top:2px}
.resumebar .rchips{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
.resumebar .rchips i{font-style:normal;padding:2px 7px;border-radius:5px;background:#18233d;
  color:#a9c0e6;font-size:11px}
.resumebar .rtop{color:#9fb2d4;font-size:11px;margin-top:6px;line-height:1.7}
.btn{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:8px 15px;font-size:13px;
  cursor:pointer;font-family:inherit;transition:.15s}
.btn:hover{filter:brightness(1.12)}
.btn.ghost{background:#1b2740;border:1px solid var(--line);color:var(--text)}
.btn.sm{padding:5px 10px;font-size:12px}
.btn:disabled{opacity:.5;cursor:not-allowed}
.workbench{display:grid;grid-template-columns:330px 1fr;gap:16px;margin-bottom:16px}
.chatlist{max-height:460px;overflow:auto}
.conv{padding:11px 13px;border-bottom:1px solid var(--line);cursor:pointer;border-left:3px solid transparent}
.conv:hover{background:#16203a}
.conv.sel{background:#152444;border-left-color:var(--accent)}
.conv .cn{display:flex;justify-content:space-between;gap:8px;font-size:13px}
.conv .cn b{font-weight:600}
.conv .cn span{color:var(--muted);font-size:11px;white-space:nowrap}
.conv .ct{color:var(--muted);font-size:11px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.conv .cl{color:#9fb2d4;font-size:12px;margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chatdetail{max-height:460px;overflow:auto;padding-right:4px}
.msg{margin-bottom:12px;display:flex;flex-direction:column;max-width:82%}
.msg.me{align-self:flex-end;align-items:flex-end}
.msg.them{align-self:flex-start;align-items:flex-start}
.msg .bub{padding:9px 12px;border-radius:10px;background:#1a2b47;font-size:13px;white-space:pre-wrap;line-height:1.7}
.msg.me .bub{background:linear-gradient(135deg,#3455a8,#4f7cff)}
.msg.system{align-self:center;color:var(--muted);font-size:12px}
.msg .tm{color:#6780a8;font-size:11px;margin-top:3px}
.greet-box{display:flex;gap:8px;margin-bottom:12px}
.greet-box input{flex:1}
.greet-tip{color:var(--muted);font-size:12px;margin-bottom:10px;line-height:1.6}
.rowbtn{padding:4px 9px;font-size:11px;border-radius:6px;border:1px solid #2c4a8a;background:#14213c;
  color:var(--cyan);cursor:pointer;white-space:nowrap}
.rowbtn:hover{background:#1c3055}
.rowbtn.done{color:var(--green);border-color:#1e5a44;background:#102a22;cursor:default}
/* 持续更新：刷新状态 + 行业趋势 */
.refreshbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:11px 16px;margin-bottom:18px;
  border:1px solid var(--line);border-radius:12px;background:linear-gradient(100deg,#0f1b33,#0b1322)}
.refreshbar .up{color:var(--muted);font-size:12px;flex:1;min-width:170px}
.refreshbar .up b{color:var(--text);font-weight:600}
.applybar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:11px 16px;margin-bottom:18px;
  border:1px solid #4a3a1f;border-radius:12px;background:#1a160e}
.applybar .up{color:var(--muted);font-size:12px;flex:1;min-width:180px}
.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--cyan);margin-right:6px;
  box-shadow:0 0 0 0 #22d3ee88;animation:pulse 1.6s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 8px #22d3ee00}100%{box-shadow:0 0 0 0 #22d3ee00}}
.trendkpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.tk{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tk .k{color:var(--muted);font-size:11px}
.tk .v{font-size:21px;font-weight:700;margin-top:3px}
.tk .d{font-size:11px;margin-top:2px}
.up-d{color:var(--pink)} .down-d{color:var(--green)} .flat-d{color:var(--muted)}
.trendgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.trendpanel{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:0}
.trendpanel h4{margin:0 0 6px;font-size:12px;color:var(--muted);font-weight:600;display:flex;justify-content:space-between;gap:8px}
.trendpanel h4 .unit{color:#5b7199;font-weight:400}
.trendpanel svg{width:100%;height:118px;display:block}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:11px;color:var(--muted)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.trendsplit{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}
.trendsplit .mini h3{margin:0 0 10px;font-size:12px;color:var(--muted);font-weight:600}
.delta{display:grid;grid-template-columns:minmax(90px,130px) 1fr 52px;gap:8px;align-items:center;
  font-size:11px;margin-bottom:7px}
.delta .nm{color:var(--muted);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.delta .pair{display:flex;gap:3px;height:9px;align-items:center}
.delta .pair i{display:block;height:9px;border-radius:2px}
.delta .pair i.old{background:#2b3d63} .delta .pair i.new{background:linear-gradient(90deg,var(--accent),var(--cyan))}
.delta .dv{text-align:right;font-variant-numeric:tabular-nums}
.newjob{padding:8px 10px;border-bottom:1px solid var(--line);font-size:12px}
.newjob:last-child{border-bottom:none}
.newjob b{color:var(--text)}
.newjob .meta{color:var(--muted);font-size:11px;margin-top:2px}
@media(max-width:900px){.workbench,.trendsplit{grid-template-columns:1fr}}
@media(max-width:700px){.grid,.trendgrid{grid-template-columns:1fr}.wrap{padding:16px 12px 50px}h1{font-size:22px}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1><span>__TITLE__</span> · 岗位数据看板</h1>
    <div class="sub">样本 __N__ 条 · JD 覆盖 __JD__ 条 · 采集于 __TIME__ <span class="tag">个人自用</span></div>
  </div>
  <div class="sub">项目：<a href="https://github.com/duanju9/scion-and-seek" target="_blank" rel="noopener">Scion &amp; Seek</a> · 来源：BOSS 直聘公开搜索页 · 本地采集</div>
</header>

<div class="loginbar" id="loginbar">
  <span class="dot" id="dot"></span>
  <span class="msg" id="loginmsg">正在检测浏览器与登录状态…</span>
  <button class="btn ghost sm" id="btnStatus">刷新状态</button>
  <button class="btn" id="btnLogin">打开登录页 / 引导登录</button>
</div>

<div class="resumebar" id="resumebar">
  <div class="ricon">CV</div>
  <div class="rmain">
    <div class="rtitle">个人简历 <span id="resumeName">未上传</span></div>
    <div class="rsub" id="resumeMsg">上传 PDF / DOCX / TXT / MD，本地解析技能、年限、学历，并生成 Top 匹配（不上传云端）</div>
    <div class="rchips" id="resumeChips"></div>
    <div class="rtop" id="resumeTop"></div>
  </div>
  <input type="file" id="resumeFile" accept=".pdf,.docx,.txt,.md" style="display:none">
  <button class="btn" id="btnResumeUpload">上传简历</button>
  <button class="btn ghost sm" id="btnResumeRematch">重新匹配</button>
</div>

<div class="refreshbar" id="refreshbar">
  <span class="up" id="updatetime">正在读取更新状态…</span>
  <span class="up" id="refreshmsg"></span>
  <button class="btn ghost sm" id="btnRefresh">立即刷新数据</button>
</div>

<div class="applybar" id="applybar">
  <span class="up" id="applymsg">自动投递：读取状态中…</span>
  <button class="btn ghost sm" id="btnApplyToggle">启用自动投递</button>
  <button class="btn sm" id="btnApplyNow">立即投递匹配岗位</button>
</div>

<div class="panel" style="margin-bottom:16px">
  <h2>行业岗位趋势 · 持续更新</h2>
  <div class="trendkpis" id="trendkpis"></div>
  <div id="trendChart"></div>
  <div class="legend" id="trendLegend"></div>
  <div class="trendsplit">
    <div class="mini">
      <h3>技能需求变化（上期 → 本期）</h3>
      <div id="skillTrend"></div>
    </div>
    <div class="mini">
      <h3>行业岗位数变化</h3>
      <div id="industryTrend"></div>
    </div>
  </div>
  <div class="mini" style="margin-top:16px">
    <h3>本期新增岗位</h3>
    <div id="newJobs"></div>
  </div>
</div>

<div class="workbench">
  <div class="panel">
    <h2>对话记录 · 会话列表</h2>
    <div class="chatlist" id="chatlist"><div class="empty">点击「刷新状态」后加载</div></div>
  </div>
  <div class="panel">
    <h2>对话详情 · <span id="who">未选择</span></h2>
    <div class="chatdetail" id="chatdetail"><div class="empty">从左侧选一个会话查看消息</div></div>
  </div>
</div>

<div class="cards" id="cards"></div>
<div class="grid" id="charts"></div>

<div class="panel" style="margin-bottom:16px">
  <h2>技能标签（出现次数）</h2>
  <div class="chips" id="skillchips"></div>
</div>

<div class="panel">
  <h2>岗位明细 · 点击任意行展开职位描述（JD）</h2>
  <div class="controls">
    <input type="search" id="q" placeholder="搜索岗位 / 公司 / 技能 / JD 关键词">
    <select id="fexp"><option value="">全部经验</option></select>
    <select id="fedu"><option value="">全部学历</option></select>
    <select id="fcity"><option value="">全部城市</option></select>
    <select id="fscale"><option value="">全部规模</option></select>
    <select id="factive"><option value="">全部活跃度</option></select>
    <span class="count" id="cnt"></span>
  </div>
  <div class="tblwrap">
    <table id="tbl">
      <thead><tr>
        <th data-k="title">岗位</th><th data-k="salary">薪资</th>
        <th data-k="city">城市</th><th data-k="exp">经验</th>
        <th data-k="edu">学历</th><th data-k="company">公司</th>
        <th data-k="scale">规模</th>        <th data-k="active">Boss 活跃</th>
        <th data-k="skills">技能</th>
        <th>操作</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>

<footer>数据仅供个人求职参考 · 招聘信息以平台实时页面为准 · 点击公司/岗位标题可跳转原页面<br>
项目：<a href="https://github.com/duanju9/scion-and-seek" target="_blank" rel="noopener">Scion &amp; Seek</a> · 代码公开，简历与岗位数据仅留本地</footer>
</div>

<script>
const JOBS = __DATA__;
const TRENDS = __TRENDS__;
const greeted = new Set();   // 已招呼的 job_id（须在 render 之前声明）
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function split(v){ return String(v||'').split('|').map(x=>x.trim()).filter(Boolean); }
function norm(j){
  const labels = split(j.job_labels);
  return {
    ...j,
    city: String(j.location||'').split('·')[0].trim(),
    exp: labels.find(x=>/年|不限|应届|在校/.test(x)) || '',
    edu: labels.find(x=>/本科|大专|硕士|高中|学历不限|中专|初中/.test(x)) || '',
    active: j.boss_active_status || '',
    skillsArr: split(j.skills)
  };
}
const rows = JOBS.map(norm);

// 指标卡
function salaryMid(s){
  const m = String(s||'').match(/(\d+)-(\d+)K/);
  return m ? (Number(m[1])+Number(m[2]))/2 : null;
}
const mids = rows.map(j=>salaryMid(j.salary)).filter(x=>x!=null);
const med = mids.length ? statisticsMedian(mids) : 0;
function statisticsMedian(a){ const b=[...a].sort((x,y)=>x-y); return b.length%2?b[(b.length-1)/2]:(b[b.length/2-1]+b[b.length/2])/2; }
const activeN = rows.filter(j=>/刚刚活跃|在线|今日活跃/.test(j.active)).length;
const companyN = new Set(rows.map(j=>j.boss_name)).size;
$('#cards').innerHTML = [
  ['c1','岗位总数',rows.length,'条'],
  ['c2','JD 覆盖率',Math.round(rows.filter(j=>j.has_jd==='yes').length/rows.length*100),'%'],
  ['c3','薪资中位数',med.toFixed(1),'K'],
  ['c4','Boss 近期活跃',Math.round(activeN/rows.length*100),'%'],
  ['c1','覆盖公司',companyN,'家']
].map(([c,k,v,u])=>`<div class="card ${c}"><div class="k">${k}</div><div class="v">${v}<small> ${u}</small></div></div>`).join('');

// 图表
function counter(key, multi, top){
  const c = new Map();
  rows.forEach(j=>{
    const vals = multi ? split(j[key]) : [j[key]];
    vals.filter(Boolean).forEach(v=>c.set(v,(c.get(v)||0)+1));
  });
  return [...c.entries()].sort((a,b)=>b[1]-a[1]).slice(0, top||99);
}
function barPanel(title, data, unit){
  const max = Math.max(1, ...data.map(d=>d[1]));
  const bars = data.map(([k,v])=>`<div class="bar"><div class="lbl" title="${esc(k)}">${esc(k)}</div>`+
    `<div class="track"><div class="fill" style="width:${(v/max*100).toFixed(1)}%"></div></div>`+
    `<div class="num">${v}${unit||''}</div></div>`).join('');
  return `<div class="panel"><h2>${title}</h2><div class="bars">${bars||'<div class="empty">无数据</div>'}</div></div>`;
}
$('#charts').innerHTML = [
  barPanel('薪资区间分布', counter('salary',false,14)),
  barPanel('经验要求', counter('exp',false,8)),
  barPanel('学历要求', counter('edu',false,8)),
  barPanel('城市 Top 12', counter('city',false,12)),
  barPanel('公司规模', counter('company_scale',false,8)),
  barPanel('融资阶段', counter('company_stage',false,8)),
  barPanel('行业 Top 12', counter('company_industry',false,12)),
  barPanel('Boss 活跃度', counter('active',false,8)),
  barPanel('福利 Top 12', counter('welfare',true,12))
].join('');
$('#skillchips').innerHTML = counter('skillsArr',true,30).map(([k,v],i)=>
  `<span class="chip ${i<6?'hot':''}">${esc(k)}<b>${v}</b></span>`).join('');

// 筛选器
function fillSel(id, vals){ const el=$(id); vals.forEach(v=>{ const o=document.createElement('option');o.value=v;o.textContent=v;el.appendChild(o);}); }
fillSel('#fexp', [...new Set(rows.map(j=>j.exp).filter(Boolean))].sort());
fillSel('#fedu', [...new Set(rows.map(j=>j.edu).filter(Boolean))].sort());
fillSel('#fcity', [...new Set(rows.map(j=>j.city).filter(Boolean))].sort());
fillSel('#fscale', [...new Set(rows.map(j=>j.company_scale).filter(Boolean))].sort());
fillSel('#factive', [...new Set(rows.map(j=>j.active).filter(Boolean))].sort());

let sortKey='salary', sortDir=-1, openId=null;
function filtered(){
  const q=$('#q').value.trim().toLowerCase();
  const f=[['#fexp','exp'],['#fedu','edu'],['#fcity','city'],['#fscale','company_scale'],['#factive','active']];
  return rows.filter(j=>{
    for(const [sel,key] of f){ const v=$(sel).value; if(v && j[key]!==v) return false; }
    if(!q) return true;
    return [j.title,j.company,j.boss_name,j.city,j.skills,j.jd].some(x=>String(x||'').toLowerCase().includes(q));
  });
}
function val(j,k){
  if(k==='company') return j.boss_name;
  if(k==='skills') return j.skills;
  if(k==='active') return j.active;
  if(k==='salary'){ const m=String(j.salary).match(/(\d+)-(\d+)K/); return m?Number(m[2]):0; }
  return j[k]||'';
}
function render(){
  let list=filtered();
  list.sort((a,b)=>{ const x=val(a,sortKey),y=val(b,sortKey);
    return (typeof x==='number' ? x-y : String(x).localeCompare(String(y),'zh'))*sortDir; });
  $('#cnt').textContent=`显示 ${list.length} / ${rows.length} 条`;
  if(!list.length){ $('#tbody').innerHTML='<tr><td colspan="10" class="empty">没有匹配的岗位</td></tr>'; return; }
  $('#tbody').innerHTML=list.map(j=>{
    const skills=j.skillsArr.slice(0,4).map(s=>`<span class="pill ${/Python|大模型|机器学习|深度学习|算法工程化/.test(s)?'hi':''}">${esc(s)}</span>`).join('');
    const done=greeted.has(j.job_id);
    const btn=done ? '<button class="rowbtn done">已招呼</button>'
      : `<button class="rowbtn" data-greet="${esc(j.job_id)}">打招呼</button>`;
    const main=`<tr class="row" data-id="${esc(j.job_id)}">
      <td><a href="${esc(j.job_link)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(j.title)}</a></td>
      <td class="sal">${esc(j.salary)}</td>
      <td>${esc(j.city)}</td><td>${esc(j.exp)}</td><td>${esc(j.edu)}</td>
      <td class="co" title="${esc(j.boss_name)}"><a href="${esc(j.company_link)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(j.boss_name)}</a></td>
      <td>${esc(j.company_scale)}</td>
      <td class="${/刚刚活跃|在线/.test(j.active)?'active':''}">${esc(j.active)}</td>
      <td>${skills}</td>
      <td>${btn}</td></tr>`;
    const detail=openId===j.job_id ? `<tr class="jdrow"><td colspan="10"><b>JD 全文 · ${esc(j.title)} @ ${esc(j.boss_name)}</b>\n\n${esc(j.jd||'（无描述）')}\n\n<span style="color:#4f7cff">打开原页面：${esc(j.job_link)}</span></td></tr>`:'';
    return main+detail;
  }).join('');
}
document.querySelectorAll('th[data-k]').forEach(th=>th.addEventListener('click',()=>{
  const k=th.dataset.k; sortDir = sortKey===k ? -sortDir : -1; sortKey=k; render();
}));
$('#tbody').addEventListener('click',async e=>{
  const gb=e.target.closest('[data-greet]');
  if(gb){ e.stopPropagation(); await doGreet(gb.dataset.greet); return; }
  const tr=e.target.closest('tr.row'); if(!tr) return;
  openId = openId===tr.dataset.id ? null : tr.dataset.id; render();
});
['#q','#fexp','#fedu','#fcity','#fscale','#factive'].forEach(s=>{
  $(s).addEventListener(s==='#q'?'input':'change',()=>{openId=null;render();});
});
render();

/* ===== 行业岗位趋势：快照时间序列 / 环比 / 新增岗位 ===== */
function dcls(v){ return Number(v)>0?'up-d':(Number(v)<0?'down-d':'flat-d'); }
function dtext(v,unit){ const n=Number(v)||0; return (n>0?'+':'')+n+(unit||''); }
function renderTrends(t){
  if(!t || !t.summaries || !t.summaries.length){
    $('#trendkpis').innerHTML='<div class="empty">暂无历史快照，刷新一次后开始记录趋势</div>';
    $('#trendChart').innerHTML=''; $('#skillTrend').innerHTML='';
    $('#industryTrend').innerHTML=''; $('#newJobs').innerHTML='';
    return;
  }
  const L=t.latest||{}, d=t.delta||{};
  const kpis=[
    ['岗位总数', L.count||0, dtext(d.job_count,' 条'), dcls(d.job_count)],
    ['薪资中位数', (L.salary_mid||0)+'K', dtext(d.salary_mid,'K'), dcls(d.salary_mid)],
    ['新增岗位', (t.new_jobs||[]).length, t.snapshot_count>1?'较上期':'首期基线', ''],
    ['Boss 活跃率', (L.active_rate||0)+'%', dtext(d.active_rate,'pp'), dcls(d.active_rate)],
    ['快照期数', t.snapshot_count, '自动留档', '']
  ];
  $('#trendkpis').innerHTML=kpis.map(([k,v,x,c])=>
    `<div class="tk"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="d ${c}">${esc(x)}</div></div>`).join('');

  // 分面折线：每个指标独立纵轴 + 真实数值，避免不同量纲归一后线条重叠误读
  const S=t.series||{}, keys=Object.keys(S).filter(k=>S[k]&&S[k].length);
  const names={job_count:'岗位量',salary_mid:'薪资中位数',new_jobs:'新增岗位',active_rate:'活跃率'};
  const colors={job_count:'#22d3ee',salary_mid:'#fbbf24',new_jobs:'#f472b6',active_rate:'#34d399'};
  const units={job_count:'条',salary_mid:'K',new_jobs:'条',active_rate:'%'};
  const n=Math.max(0,...keys.map(k=>S[k].length));
  if(!n){ $('#trendChart').innerHTML='<div class="empty">暂无时间序列</div>'; return; }
  const W=460,H=118,pL=34,pR=30,pT=10,pB=20, iw=W-pL-pR, ih=H-pT-pB;
  const fmt=v=>Number.isInteger(v)?String(v):Number(v).toFixed(1);
  const panels=keys.map(k=>{
    const arr=S[k], vals=arr.map(a=>Number(a.v)||0);
    let mn=Math.min(...vals), mx=Math.max(...vals);
    if(mx===mn){ const pad=Math.max(0.5,Math.abs(mx)*0.02); mn-=pad; mx+=pad; }
    else { const pad=(mx-mn)*0.16; mn-=pad; mx+=pad; }
    if(Math.min(...vals)>=0 && mn<0) mn=0;
    const xAt=i=>arr.length===1?pL+iw/2:pL+iw*i/(arr.length-1);
    const yAt=v=>pT+ih*(1-(v-mn)/((mx-mn)||1));
    let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(names[k]||k)}趋势">`;
    for(let g=0;g<=2;g++){
      const y=pT+ih*g/2, val=mx-(mx-mn)*g/2;
      svg+=`<line x1="${pL}" y1="${y.toFixed(1)}" x2="${W-pR}" y2="${y.toFixed(1)}" stroke="#1f2b45" stroke-width="1"/>`;
      svg+=`<text x="${pL-4}" y="${(y+3).toFixed(1)}" fill="#7287ad" font-size="9" text-anchor="end">${esc(fmt(val))}</text>`;
    }
    const pts=arr.map((a,i)=>`${xAt(i).toFixed(1)},${yAt(Number(a.v)||0).toFixed(1)}`).join(' ');
    svg+=`<polyline points="${pts}" fill="none" stroke="${colors[k]||'#4f7cff'}" stroke-width="2" stroke-linejoin="round"/>`;
    arr.forEach((a,i)=>{
      const x=xAt(i), y=yAt(Number(a.v)||0);
      svg+=`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.2" fill="${colors[k]||'#4f7cff'}"><title>${esc(a.ts||'')} ${names[k]||k}: ${esc(a.v)}</title></circle>`;
      if(arr.length<=3 || i===arr.length-1){
        const anchor=i===0?'start':(i===arr.length-1?'end':'middle');
        svg+=`<text x="${x.toFixed(1)}" y="${Math.max(pT+8,y-7).toFixed(1)}" fill="#c9d6ee" font-size="10" text-anchor="${anchor}">${esc(fmt(a.v))}</text>`;
      }
    });
    [0,arr.length-1].filter((v,i,a)=>a.indexOf(v)===i).forEach(i=>{
      const lbl=String(arr[i].ts||'').replace('T',' ').slice(5,16);
      svg+=`<text x="${xAt(i).toFixed(1)}" y="${H-5}" fill="#8fa3c8" font-size="9" text-anchor="${i===0?'start':'end'}">${esc(lbl)}</text>`;
    });
    svg+='</svg>';
    return `<div class="trendpanel"><h4><span>${esc(names[k]||k)}</span><span class="unit">${esc(units[k]||'')}</span></h4>${svg}</div>`;
  });
  $('#trendChart').innerHTML='<div class="trendgrid">'+panels.join('')+'</div>';
  $('#trendLegend').innerHTML='<span style="color:#5b7199">每个指标独立纵轴，标签为真实数值；不同面板的高度不可直接横向比较。</span>';

  // 技能环比：取最后一期 Top8
  const ss=t.summaries;
  const cur=ss[ss.length-1].skills||{}, old=ss.length>1?(ss[ss.length-2].skills||{}):{};
  const topNames=Object.keys(cur).slice(0,8);
  const smax=Math.max(1,...topNames.map(k=>Math.max(cur[k]||0,old[k]||0)));
  $('#skillTrend').innerHTML=topNames.map(k=>{
    const a=old[k]||0,b=cur[k]||0,dd=b-a;
    return `<div class="delta"><span class="nm" title="${esc(k)}">${esc(k)}</span>`+
      `<span class="pair"><i class="old" style="width:${(a/smax*100).toFixed(1)}%"></i>`+
      `<i class="new" style="width:${(b/smax*100).toFixed(1)}%"></i></span>`+
      `<span class="dv ${dcls(dd)}">${dtext(dd)}</span></div>`;
  }).join('')||'<div class="empty">无技能数据</div>';

  const ind=(t.industry_delta||[]).slice(0,8);
  const imax=Math.max(1,...ind.flatMap(x=>[x.now||0,x.prev||0]));
  $('#industryTrend').innerHTML=ind.map(x=>
    `<div class="delta"><span class="nm" title="${esc(x.name)}">${esc(x.name)}</span>`+
    `<span class="pair"><i class="old" style="width:${((x.prev||0)/imax*100).toFixed(1)}%"></i>`+
    `<i class="new" style="width:${((x.now||0)/imax*100).toFixed(1)}%"></i></span>`+
    `<span class="dv ${dcls(x.delta)}">${dtext(x.delta)}</span></div>`).join('')||'<div class="empty">至少两期快照后显示环比</div>';

  $('#newJobs').innerHTML=(t.new_jobs||[]).slice(0,12).map(j=>
    `<div class="newjob"><b>${esc(j.title)}</b> · ${esc(j.salary)} · ${esc(j.boss_name)}`+
    `<div class="meta">${esc(String(j.skills||'').split('|').slice(0,5).join(' · '))} · ${esc(j.location||'')}</div></div>`).join('')
    ||'<div class="empty">本期无新增岗位（或仅有一期快照）</div>';
}
renderTrends(TRENDS);

/* ===== 工作台：登录引导 / 对话记录 / 招呼联动 ===== */
let currentWho = '';
let loginHoldUntil = 0;
const API = (path, opts={}, ms=15000) => {
  const ctl=new AbortController();
  const timer=setTimeout(()=>ctl.abort(), ms);
  return fetch(path,{...opts,signal:ctl.signal})
    .then(async r=>{
      const text=await r.text();
      try{ return JSON.parse(text); }
      catch(e){ return {ok:false,msg:'服务返回异常（HTTP '+r.status+'）'}; }
    })
    .finally(()=>clearTimeout(timer));
};
const WORKBENCH_URL='http://127.0.0.1:8765/';
const IN_WORKBENCH=(location.hostname==='127.0.0.1'||location.hostname==='localhost')&&location.port==='8765';
if(!IN_WORKBENCH){
  const link=`<a href="${WORKBENCH_URL}" target="_blank">${WORKBENCH_URL}</a>`;
  $('#loginmsg').innerHTML='当前是静态预览，不能调用浏览器与本地数据。请从 '+link+' 打开工作台。';
  $('#resumeMsg').innerHTML='静态预览无法上传简历，请从 '+link+' 打开工作台后上传。';
  $('#btnLogin').disabled=true; $('#btnStatus').disabled=true;
  $('#btnRefresh').disabled=true; $('#btnApplyToggle').disabled=true; $('#btnApplyNow').disabled=true;
  $('#btnResumeUpload').disabled=true; $('#btnResumeRematch').disabled=true;
}

async function refreshStatus(){
  try{
    const s = await API('/api/status');
    const dot=$('#dot');
    dot.className = 'dot ' + (s.logged_in?'on':(s.cdp?'':'off'));
    if(Date.now() > loginHoldUntil) $('#loginmsg').textContent = s.message || '状态未知';
    $('#btnLogin').textContent = s.logged_in ? '重新打开职位页' : '打开登录页 / 引导登录';
    if(s.logged_in) await loadConversations();
  }catch(e){
    $('#dot').className='dot off';
    $('#loginmsg').innerHTML=IN_WORKBENCH
      ? '本地服务未响应：请确认 dashboard_server.py 正在运行'
      : '当前是静态预览，不能调用本地能力。请从 <a href="'+WORKBENCH_URL+'" target="_blank">'+WORKBENCH_URL+'</a> 打开工作台。';
  }
}
$('#btnStatus').addEventListener('click', refreshStatus);
$('#btnLogin').addEventListener('click', async ()=>{
  const btn=$('#btnLogin'), old=btn.textContent;
  btn.disabled=true; btn.textContent='正在打开…';
  $('#loginmsg').textContent='正在打开 BOSS 职位页…';
  try{
    const r=await API('/api/login',{method:'POST'},10000);
    loginHoldUntil=Date.now()+8000;
    $('#loginmsg').textContent=r.msg||'请在专用 Chrome 窗口中完成登录';
    setTimeout(refreshStatus, 1500);
  }catch(e){
    const timeout=e && e.name==='AbortError';
    $('#loginmsg').textContent=timeout
      ? '打开登录页超时：专用 Chrome 可能正在采集。请等刷新结束后重试，或直接切到 Chrome 窗口。'
      : '无法调用登录：'+e;
  }finally{
    btn.disabled=false; btn.textContent=old;
  }
});

async function loadConversations(){
  try{
    const r=await API('/api/conversations');
    if(!r.ok){ $('#chatlist').innerHTML=`<div class="empty">${esc(r.msg||'加载失败')}</div>`; return; }
    if(!r.items.length){ $('#chatlist').innerHTML='<div class="empty">暂无会话，打招呼后会出现在这里</div>'; return; }
    $('#chatlist').innerHTML=r.items.map(c=>`
      <div class="conv ${c.selected?'sel':''}" data-who="${esc(c.name)}" data-title="${esc(c.title)}">
        <div class="cn"><b>${esc(c.name)}</b><span>${esc(c.time)}</span></div>
        <div class="ct">${esc(c.title)}</div>
        <div class="cl">${esc(c.last)}</div>
      </div>`).join('');
    document.querySelectorAll('.conv').forEach(el=>el.addEventListener('click',()=>{
      document.querySelectorAll('.conv').forEach(x=>x.classList.remove('sel'));
      el.classList.add('sel');
      loadMessages(el.dataset.who, el.dataset.title);
    }));
  }catch(e){ $('#chatlist').innerHTML='<div class="empty">加载失败：'+esc(e)+'</div>'; }
}

let msgSeq=0;
async function loadMessages(who, title){
  const seq=++msgSeq;
  currentWho=who;
  $('#who').textContent=title||who;
  $('#chatdetail').innerHTML='<div class="empty">正在切换会话，BOSS 消息有 5 秒左右延迟…</div>';
  try{
    const r=await API('/api/messages?who='+encodeURIComponent(who));
    if(seq!==msgSeq) return;
    if(!r.ok || !r.messages.length){ $('#chatdetail').innerHTML=`<div class="empty">${esc(r.msg||'暂无消息')}</div>`; return; }
    $('#chatdetail').innerHTML=r.messages.map(m=>{
      if(m.role==='system') return `<div class="msg system">${esc(m.text)}</div>`;
      return `<div class="msg ${m.role}"><div class="bub">${esc(m.text)}</div><div class="tm">${esc(m.time)}</div></div>`;
    }).join('');
    $('#chatdetail').scrollTop=$('#chatdetail').scrollHeight;
  }catch(e){ if(seq===msgSeq) $('#chatdetail').innerHTML='<div class="empty">加载失败：'+esc(e)+'</div>'; }
}

async function doGreet(jobId){
  const j=rows.find(x=>x.job_id===jobId); if(!j) return;
  const btn=document.querySelector(`[data-greet="${jobId}"]`);
  if(btn) btn.disabled=true;
  const def=`您好，看到${j.boss_name}「${j.title}」岗位很感兴趣。我有相关经验，主要使用${j.skillsArr.slice(0,4).join('、')||'相关技术'}，与岗位要求比较契合。方便的话想了解下团队目前的工作重点？`;
  const msg=window.prompt(`发送给 ${j.boss_name}（${j.title}）的招呼语：`, def);
  if(msg===null){ if(btn) btn.disabled=false; return; }
  if(btn) btn.textContent='发送中…';
  try{
    const r=await API('/api/greet',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({job_url:j.job_link,recipient:j.boss_name,greeting:msg})});
    if(r.ok){ greeted.add(jobId); alert('招呼语已送达 '+r.recipient); loadConversations(); }
    else { alert('发送失败：'+r.msg); }
  }catch(e){ alert('调用失败：'+e); }
  render();
}

/* ===== 持续更新：刷新状态轮询，数据变更自动重载 ===== */
let pageVersion=null, refreshing=false;
async function pollRefresh(){
  try{
    const s=await API('/api/refresh/status');
    const bits=[];
    if(s.last_ok_at) bits.push('最后更新 <b>'+esc(s.last_ok_at)+'</b>');
    if(s.next_at && s.status!=='running') bits.push('下次自动刷新 '+esc(s.next_at.slice(11,16)));
    if(s.snapshot_count) bits.push('已留档 '+s.snapshot_count+' 期');
    $('#updatetime').innerHTML=bits.join(' · ')||esc(s.msg||'等待首次刷新');
    const last=(s.steps||[]).filter(x=>x.startsWith('[')).slice(-1)[0]||'';
    if(s.status==='running'){
      $('#refreshmsg').innerHTML='<span class="pulse"></span>'+esc(last||'正在刷新…');
      refreshing=true; $('#btnRefresh').disabled=true;
    }else{
      if(refreshing){ $('#refreshmsg').textContent=s.status==='ok'?'刷新完成':'刷新失败：'+(s.error||''); refreshing=false; }
      $('#btnRefresh').disabled=false;
    }
    if(s.data_version){
      if(pageVersion && s.data_version!==pageVersion){ location.reload(); return; }
      pageVersion=s.data_version;
    }
  }catch(e){
    $('#updatetime').innerHTML=IN_WORKBENCH
      ? '本地服务未响应，刷新功能暂不可用'
      : '离线模式 · 数据已内嵌；<a href="'+WORKBENCH_URL+'" target="_blank">打开工作台</a>后可自动更新';
    $('#btnRefresh').disabled=true;
  }
}
$('#btnRefresh').addEventListener('click', async ()=>{
  const b=$('#btnRefresh'); b.disabled=true;
  $('#refreshmsg').innerHTML='<span class="pulse"></span>已提交刷新任务…';
  try{
    const r=await API('/api/refresh',{method:'POST'});
    $('#refreshmsg').textContent=r.msg||'刷新任务已启动';
  }catch(e){ $('#refreshmsg').textContent='无法启动刷新：'+e; b.disabled=false; }
  setTimeout(pollRefresh, 1500);
});

/* ===== 持续自动投递：启用状态 / 进度 / 去重计数 ===== */
let applyEnabled=false;
async function pollApply(){
  try{
    const s=await API('/api/auto-apply/status');
    applyEnabled=!!s.enabled;
    const bits=['自动投递 <b>'+(s.enabled?'已启用':'未启用')+'</b>'];
    if(s.applied_count) bits.push('累计投递 '+s.applied_count+' 条');
    if(s.last_at) bits.push('上次 '+esc(s.last_at.slice(11,16)));
    if(s.status==='running') bits.push('进度 '+s.progress+'/'+s.total);
    if(s.error) bits.push('<span style="color:#f87171">'+esc(s.error)+'</span>');
    else if(s.msg) bits.push(esc(s.msg));
    $('#applymsg').innerHTML=bits.join(' · ');
    $('#btnApplyToggle').textContent=s.enabled?'关闭自动投递':'启用自动投递';
    $('#btnApplyToggle').disabled=s.status==='running';
    $('#btnApplyNow').disabled=!s.enabled||s.status==='running';
  }catch(e){
    $('#applymsg').innerHTML=IN_WORKBENCH
      ? '自动投递：本地服务未响应'
      : '自动投递：<a href="'+WORKBENCH_URL+'" target="_blank">打开工作台</a>后可启用';
    $('#btnApplyToggle').disabled=true; $('#btnApplyNow').disabled=true;
  }
}
$('#btnApplyToggle').addEventListener('click',async ()=>{
  const want=!applyEnabled;
  if(want && !confirm('启用后会按简历匹配结果，在每次数据刷新后自动发送招呼语（有去重和限速）。确认启用？')) return;
  try{
    const r=await API('/api/auto-apply/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:want})});
    $('#applymsg').textContent=r.msg||'状态已更新';
  }catch(e){ $('#applymsg').textContent='配置失败：'+e; }
  setTimeout(pollApply,500);
});
$('#btnApplyNow').addEventListener('click',async ()=>{
  try{
    const r=await API('/api/auto-apply',{method:'POST'});
    $('#applymsg').textContent=r.msg||'投递任务已启动';
  }catch(e){ $('#applymsg').textContent='启动失败：'+e; }
  setTimeout(pollApply,1000);
});

/* ===== 个人简历：上传 / 解析 / 匹配结果 ===== */
let resumeBusy=false;
async function pollResume(){
  if(!IN_WORKBENCH) return;
  try{
    const s=await API('/api/resume/status');
    const name=$('#resumeName'), msg=$('#resumeMsg'), chips=$('#resumeChips'), top=$('#resumeTop');
    if(!s.has_resume){
      name.textContent='未上传';
      msg.textContent='上传 PDF / DOCX / TXT / MD，本地解析技能、年限、学历，并生成 Top 匹配（不上传云端）';
      chips.innerHTML=''; top.innerHTML=''; $('#btnResumeRematch').disabled=true; return;
    }
    name.textContent=s.name||'已上传';
    msg.innerHTML=`已解析 ${esc(s.parsed_at||'')} · ${s.years_experience?s.years_experience+' 年':'年限未识别'} · ${esc((s.education||[]).join('/')||'学历未识别')}`+
      (s.is_test?' · <span style="color:#fbbf24">测试样本，不能用于自动投递</span>':'');
    chips.innerHTML=(s.skills||[]).slice(0,12).map(x=>`<i>${esc(x)}</i>`).join('');
    top.innerHTML=(s.top_matches||[]).length
      ? 'Top 匹配：'+s.top_matches.map(m=>`${esc(m.title)}（${esc(m.company)}，${esc(m.score)} 分）`).join(' · ')
      : '尚未生成匹配；刷新岗位后点「重新匹配」';
    $('#btnResumeRematch').disabled=false;
  }catch(e){
    $('#resumeMsg').textContent='简历状态读取失败：'+e;
  }
}
function readFileB64(file){
  return new Promise((resolve,reject)=>{
    const r=new FileReader();
    r.onload=()=>resolve(String(r.result).split(',')[1]||'');
    r.onerror=()=>reject('文件读取失败');
    r.readAsDataURL(file);
  });
}
$('#btnResumeUpload').addEventListener('click',()=>{
  if(!IN_WORKBENCH){ alert('请从 '+WORKBENCH_URL+' 打开工作台后再上传简历'); return; }
  if(!resumeBusy) $('#resumeFile').click();
});
$('#resumeFile').addEventListener('change',async e=>{
  const f=e.target.files[0]; if(!f) return;
  if(f.size>15*1024*1024){ alert('文件超过 15MB，请压缩后再上传'); e.target.value=''; return; }
  const btn=$('#btnResumeUpload'), old=btn.textContent;
  resumeBusy=true; btn.disabled=true; btn.textContent='解析中…';
  $('#resumeMsg').textContent='正在本地解析并匹配岗位，请稍候（不联网上传）…';
  try{
    const b64=await readFileB64(f);
    const r=await API('/api/resume/upload',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:f.name,content_b64:b64})},180000);
    if(!r.ok) alert('上传失败：'+(r.msg||'未知错误'));
    else await pollResume();
    await pollApply();
  }catch(err){ alert('上传失败：'+err); }
  resumeBusy=false; btn.disabled=false; btn.textContent=old; e.target.value='';
});
$('#btnResumeRematch').addEventListener('click',async ()=>{
  const b=$('#btnResumeRematch'); b.disabled=true; b.textContent='匹配中…';
  try{
    const r=await API('/api/resume/rematch',{method:'POST'},180000);
    if(!r.ok) alert('匹配失败：'+(r.msg||''));
    await pollResume();
  }catch(e){ alert('匹配失败：'+e); }
  b.disabled=false; b.textContent='重新匹配';
});

if(IN_WORKBENCH){
  refreshStatus();
  pollRefresh();
  pollApply();
  pollResume();
  setInterval(refreshStatus, 60000);
  setInterval(pollRefresh, 8000);
  setInterval(pollApply, 8000);
  setInterval(pollResume, 30000);
}
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="含 JD 的主表 JSON")
    ap.add_argument("--output", required=True, help="输出 HTML 路径")
    ap.add_argument("--trends", help="趋势 JSON，内嵌后离线也能看趋势")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    jobs = data["jobs"] if isinstance(data, dict) and "jobs" in data else data
    title = data.get("keyword", "岗位") if isinstance(data, dict) else "岗位"

    trends = {}
    if args.trends and os.path.isfile(args.trends):
        with open(args.trends, encoding="utf-8") as f:
            trends = json.load(f)

    payload = json.dumps(jobs, ensure_ascii=False).replace("</", "<\\/")
    trend_payload = json.dumps(trends, ensure_ascii=False).replace("</", "<\\/")
    html = (TEMPLATE.replace("__TRENDS__", trend_payload)
                    .replace("__DATA__", payload)
                    .replace("__TITLE__", title)
                    .replace("__N__", str(len(jobs)))
                    .replace("__JD__", str(sum(1 for j in jobs if j.get("has_jd") == "yes")))
                    .replace("__TIME__", data.get("scraped_at", "")[:16]
                             if isinstance(data, dict) else ""))
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"OK {args.output} ({len(html)} bytes, {len(jobs)} jobs)")


if __name__ == "__main__":
    main()
