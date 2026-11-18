// THROWAWAY: compare three desktop layouts with the same in-memory sample jobs.
// No rclone, WebDAV requests, local file access, credentials, or persistent storage.
const paths = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  folders: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v2"/><path d="M3 10h6l2 3h10v7a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  history: '<path d="M3 11a9 9 0 1 1 2.5 7.3M3 4v7h7"/><path d="M12 7v5l3 2"/>',
  activity: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  settings: '<path d="M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.5"/>',
  sync: '<path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5m-4 8a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  check: '<path d="m5 12 4 4 10-10"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  disk: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 14h18m-4 3h1m-5 0h.01"/>',
  phone: '<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 5h4m-3 14h2"/>',
  link: '<path d="m10 13 4-4m-7 6-1 1a4 4 0 0 0 6 6l4-4a4 4 0 0 0 0-6m1-3 1-1a4 4 0 0 0-6-6L8 6a4 4 0 0 0 0 6" transform="translate(0 -2)"/>',
  arrow: '<path d="M4 12h15m-5-5 5 5-5 5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  play: '<path d="m8 4 12 8-12 8Z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  edit: '<path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 14Z"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.1"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6M8 14h8m-8 3h5"/>',
  restore: '<path d="M8 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-3M12 3h9v9m0-9L11 13"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2l-1.5 1v2m0 3h.01"/>',
  bell: '<path d="M5 16h14l-2-3V8a5 5 0 0 0-10 0v5Zm5 4h4"/>',
  code: '<path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 20"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  network: '<rect x="8" y="3" width="8" height="6" rx="1"/><path d="M12 9v6M4 15h16M4 15v3m8-3v3m8-3v3"/><path d="M2 18h4v3H2zm8 0h4v3h-4zm8 0h4v3h-4z"/>',
  laptop: '<rect x="5" y="4" width="14" height="12" rx="1.5"/><path d="m5 16-3 4h20l-3-4"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.folder}</svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const initialTasks = [
  {id:'work', name:'LefeetHardware', local:'D:\\LefeetHardware', remote:'backup/work', versions:'backup/work-versions', interval:20, retention:90, preset:'recommended', enabled:true, size:'1.77 GiB', files:'2,884', last:'09:40', next:'10:00', tone:'', demo:false},
  {id:'docs', name:'产品文档', local:'D:\\工作资料\\产品文档', remote:'backup/product-docs/mirror', versions:'backup/product-docs/versions', interval:60, retention:30, preset:'none', enabled:true, size:'248 MiB', files:'126', last:'09:20', next:'10:20', tone:'sand', demo:true},
  {id:'design', name:'设计素材', local:'E:\\设计素材', remote:'backup/design-assets/mirror', versions:'backup/design-assets/versions', interval:120, retention:180, preset:'none', enabled:false, size:'683 MiB', files:'364', last:'昨天 18:30', next:'—', tone:'teal', demo:true},
];
const variants = {A:{name:'清爽工作台', detail:'任务卡片 + 状态侧栏'}, B:{name:'文件夹映射', detail:'文件夹列表 + 单向流向'}, C:{name:'紧凑控制台', detail:'批量任务表 + 执行队列'}};
const queryVariant = new URL(location.href).searchParams.get('variant')?.toUpperCase();
const state = {
  variant:variants[queryVariant] ? queryVariant : 'A', page:'tasks', filter:'all', query:'',
  tasks:structuredClone(initialTasks), selectedTask:'work', versionTask:'work', versionIndex:0,
  selected:new Set(), mounted:true, paused:false, queue:[], running:null,
  events:[
    {time:'09:40:12', task:'LefeetHardware', text:'同步完成', detail:'本轮无变化 · 无需传输', kind:'check'},
    {time:'09:20:05', task:'产品文档', text:'同步完成', detail:'更新 2 个文件 · 旧文件已保留', kind:'check'},
    {time:'09:00:01', task:'手机 NAS', text:'盘符已挂载', detail:'X: · WebDAV 连接正常（演示）', kind:'disk'},
  ],
};
let editor = null;
let toastTimer;
const timeNow = () => new Date().toLocaleTimeString('zh-CN',{hour12:false});
const taskById = id => state.tasks.find(t => t.id === id);
const selectedTask = () => taskById(state.selectedTask) || state.tasks[0];
const filteredTasks = () => state.tasks.filter(t => (state.filter !== 'active' || t.enabled) && (state.filter !== 'paused' || !t.enabled) && `${t.name} ${t.local}`.toLowerCase().includes(state.query.toLowerCase()));
const button = (label, action, style='secondary', glyph='', extra='') => `<button class="btn ${style}" data-action="${action}" ${extra}>${glyph ? icon(glyph) : ''}${label}</button>`;
const tinyButton = (label, action, glyph, id='') => `<button class="icon-btn" data-action="${action}" ${id ? `data-id="${esc(id)}"` : ''} aria-label="${esc(label)}" title="${esc(label)}">${icon(glyph)}</button>`;
const taskStatus = task => {
  if (state.running === task.id) return `<span class="badge blue"><i class="dot"></i>同步中 ${task.progress || 0}%</span>`;
  if (state.queue.includes(task.id)) return '<span class="badge amber"><i class="dot"></i>等待同步</span>';
  if (task.enabled && task.last === '从未执行') return '<span class="badge blue"><i class="dot"></i>待首次同步</span>';
  return task.enabled ? `<span class="badge good">${icon('check')}已同步</span>` : `<span class="badge neutral">${icon('pause')}已暂停</span>`;
};
const navItems = [['overview','grid','总览'],['tasks','folders','同步任务'],['versions','history','历史文件'],['activity','activity','活动记录'],['settings','settings','设置']];
function getRelevantState(){
  return {prototype:true, data:'SIMULATED ONLY', variant:state.variant, page:state.page, mounted:state.mounted, paused:state.paused, running:state.running, queue:state.queue, selectedTask:state.selectedTask, tasks:state.tasks.map(t => ({...t}))};
}
function winbar(){
  return `<div class="winbar"><span>PhoneNAS <span class="prototype-caption">&nbsp; / &nbsp; DESIGN PREVIEW</span></span><span class="prototype-caption">界面原型 · 所有状态均为模拟 · 不连接手机</span><div class="window-controls" aria-hidden="true"><span>−</span><span>□</span><span>×</span></div></div>`;
}
function searchBox(){
  return `<label class="search-box">${icon('search')}<input aria-label="搜索同步任务" placeholder="搜索任务…" value="${esc(state.query)}" data-search></label>`;
}
function taskFilters(style='segmented'){
  return `<div class="${style}">${[['all','全部',state.tasks.length],['active','已启用',state.tasks.filter(t=>t.enabled).length],['paused','已暂停',state.tasks.filter(t=>!t.enabled).length]].map(([key,label,count])=>`<button data-action="filter" data-filter="${key}" class="${state.filter===key?'active':''}" aria-pressed="${state.filter===key}">${label} <span>${count}</span></button>`).join('')}</div>`;
}
function standardHead(title, subtitle, actions=''){
  return `<header class="page-head"><div><h1>${title}</h1><p class="lede">${subtitle}</p></div><div class="page-actions">${actions}</div></header>`;
}
function render(){
  document.body.className=`variant-${state.variant.toLowerCase()}`;
  document.title=`PhoneNAS · ${state.variant} ${variants[state.variant].name} · UI 原型`;
  document.getElementById('app').innerHTML = ({A:VariantA,B:VariantB,C:VariantC}[state.variant])();
  renderSwitcher();
}
function VariantA(){
  return `${winbar()}<div class="a-shell"><aside class="a-sidebar"><div class="a-brand"><div class="brand-mark">${icon('network')}</div><div><strong>PhoneNAS</strong><div class="brand-sub">YOUR POCKET NAS</div></div></div><nav aria-label="主导航">${navItems.map(([key,glyph,label])=>`${key==='settings'?'<div class="tiny-title">偏好设置</div>':''}<button class="nav-item ${state.page===key?'active':''}" data-action="navigate" data-page="${key}">${icon(glyph)}<span>${label}</span>${key==='tasks'?`<span class="nav-count">${state.tasks.length}</span>`:''}</button>`).join('')}</nav><div class="a-device-mini"><div class="divider"></div><div class="row"><div class="device-icon" aria-hidden="true"></div><div><strong>vivo V2227A</strong><div class="status-line good-text"><i class="dot"></i>设备在线 · 演示</div></div></div><p class="small muted" style="font-size:9px;margin-top:17px">私有存储，安心随行。</p></div></aside><main class="a-body"><div class="a-topbar"><div class="breadcrumb">我的空间 &nbsp; / &nbsp; <b>${navItems.find(i=>i[0]===state.page)?.[2]}</b></div><div class="a-topbar-right"><span class="status-line">${icon('shield')}仅在本地运行的界面原型</span><button class="device-pill" data-action="navigate" data-page="overview"><i class="dot"></i>手机 NAS 已连接</button><div class="avatar" aria-hidden="true">P</div></div></div>${state.page==='tasks'?tasksA():secondaryPage()}</main></div>`;
}
function tasksA(){
  const active=state.tasks.filter(t=>t.enabled).length;
  return `${standardHead('让每个文件夹，都有备份。','把重要文件交给手机。每个文件夹，都有独立的同步计划。',button(state.paused?'继续计划':'暂停计划','pauseAll','secondary',state.paused?'play':'pause')+button('添加文件夹','add','primary','plus'))}<div class="a-summary"><div class="a-stat"><div class="a-stat-icon">${icon('folders')}</div><div><div class="value">${state.tasks.length}<small>个文件夹</small></div><p>${active} 个已启用 · ${state.tasks.length-active} 个已暂停</p></div></div><div class="a-stat"><div class="a-stat-icon">${icon('shield')}</div><div><div class="value">独立保留<small>旧文件</small></div><p>按任务设置保留天数</p></div></div><div class="a-stat"><div class="a-stat-icon">${icon('clock')}</div><div><div class="value">${state.running?'同步中':state.paused?'已暂停':'10:00'}<small>${state.running?'演示执行':state.paused?'自动计划':'下次同步'}</small></div><p>${state.running?'队列内 '+state.queue.length+' 个任务':state.paused?'手动同步仍可使用':'任务串行执行，不抢占带宽'}</p></div></div></div><div class="a-workspace"><section class="a-list-panel"><div class="a-list-toolbar">${taskFilters()}${searchBox()}</div>${filteredTasks().map(task=>`<article class="a-job"><div class="a-job-top"><div class="a-job-title"><div class="folder-icon ${task.tone}">${icon('folder')}</div><div><h3>${esc(task.name)} ${task.demo?'<span class="tag">示例</span>':''}</h3><div class="mono" title="${esc(task.local)}">${esc(task.local)}</div></div></div>${taskStatus(task)}</div><div class="a-job-meta"><span>${icon('clock')}每 ${task.interval} 分钟</span><span>${icon('history')}保留 ${task.retention} 天</span><span>${icon('disk')}${esc(task.size)}</span></div><div class="a-job-bottom"><span class="small">${state.running===task.id?'正在模拟传输，未访问手机':`上次同步 ${esc(task.last)}`}</span><div class="row" style="gap:1px">${button('预演','preview','subtle','eye',`data-id="${task.id}"`)}${button('设置','edit','subtle','settings',`data-id="${task.id}"`)}${button('同步','start','subtle','sync',`data-id="${task.id}"`)}${tinyButton(task.enabled?'暂停 '+task.name:'继续 '+task.name,'toggle',task.enabled?'pause':'play',task.id)}</div></div>${state.running===task.id?`<div class="progress"><i style="width:${task.progress||0}%"></i></div>`:''}</article>`).join('')||emptyTasks()}<button class="a-add-job" data-action="batch">${icon('plus')}一次添加多个文件夹</button><p class="a-footer-note">${icon('info')}每个任务使用独立的镜像和历史目录，彼此互不影响。</p></section><aside class="a-rail"><section class="a-rail-box"><div class="section-title"><h3>你的手机 NAS</h3><span class="badge good"><i class="dot"></i>在线</span></div><div class="a-nas-illustration" aria-hidden="true"><div class="phone-drawing">${icon('shield')}</div><div class="connection-dots"><i></i><i></i><i></i></div><div class="disk-drawing">${icon('disk')}</div></div><div class="a-nas-info"><span class="muted">设备</span><span>vivo V2227A</span></div><div class="a-nas-info"><span class="muted">网络盘</span><span class="mono">${state.mounted?'X: / PhoneNAS':'尚未挂载'}</span></div><div class="a-nas-info"><span class="muted">连接方式</span><span>Tailscale</span></div>${button(state.mounted?'打开网络盘':'挂载网络盘','openDrive','secondary','folder')}<p style="font-size:9px;color:#a2adc0;text-align:center;margin-top:10px">设备和连接状态均为演示</p></section><section class="a-rail-box"><div class="section-title"><h3>最近活动</h3><button class="text-btn" data-action="navigate" data-page="activity">全部 ${icon('chevron')}</button></div><div class="mini-events">${state.events.slice(0,3).map(e=>`<div class="mini-event">${esc(e.task)}<p>${esc(e.text)} · ${esc(e.time.slice(0,5))}</p></div>`).join('')}</div></section></aside></div>`;
}
function VariantB(){
  const selected=selectedTask();
  return `${winbar()}<header class="b-topnav"><div class="b-brand"><div class="brand-mark">${icon('folders')}</div><strong>PhoneNAS</strong></div><nav class="b-nav" aria-label="主导航">${[['tasks','文件夹'],['versions','历史文件'],['activity','活动'],['settings','设置']].map(([key,label])=>`<button class="${state.page===key?'active':''}" data-action="navigate" data-page="${key}">${label}</button>`).join('')}</nav><button class="b-connection" data-action="navigate" data-page="overview"><i class="dot"></i><span class="connection-copy">vivo V2227A · 在线</span>${icon('phone')}</button></header><div class="b-shell"><aside class="b-explorer"><div class="b-explorer-head"><h3>我的文件夹 <span class="muted">${state.tasks.length}</span></h3>${tinyButton('添加文件夹','add','plus')}</div><div class="b-tree" aria-label="同步文件夹">${state.tasks.map(task=>`<button class="b-tree-item ${selected?.id===task.id?'active':''}" data-action="selectTask" data-id="${task.id}">${icon('folder')}<div><strong>${esc(task.name)}</strong><div class="mono">${esc(task.local)}</div></div><i class="tree-dot" style="${task.enabled?'':'background:#bdc5b6'}"></i></button>`).join('')}</div><button class="b-tree-add" data-action="batch">${icon('plus')}添加多个文件夹</button><div class="b-tree-foot">一个文件夹，一份独立计划。<br>单向镜像到手机，<br>变动前的旧文件自动留存。<br><br><span class="mono" style="font-size:9px">仅演示 · 不写入文件</span></div></aside><main class="b-main">${state.page==='tasks'?tasksB(selected):secondaryPage()}</main></div>`;
}
function tasksB(task){
  if(!task) return `${standardHead('从一个重要文件夹开始。','添加你需要保护的文件夹。',button('添加文件夹','add','primary','plus'))}${emptyTasks()}`;
  return `<div class="eyebrow">FOLDER ${String(state.tasks.indexOf(task)+1).padStart(2,'0')} &nbsp; / &nbsp; ${String(state.tasks.length).padStart(2,'0')}</div>${standardHead(esc(task.name),`${task.demo?'示例文件夹 · ':''}电脑是唯一来源，手机保存当前镜像与变动前的旧文件。`,button(task.enabled?'暂停':'继续','toggle','secondary',task.enabled?'pause':'play',`data-id="${task.id}"`)+button('同步这个文件夹','start','primary','sync',`data-id="${task.id}"`))}<div class="row spread"><span class="small muted">文件流向</span>${taskStatus(task)}</div><section class="b-map" aria-label="从电脑到手机的单向映射"><article class="b-endpoint"><div class="b-endpoint-header">${icon('laptop')}这台电脑<span class="endpoint-label">来源 · 只读</span></div><div class="b-folder-glyph">${icon('folder')}</div><h3>${esc(task.name)}</h3><div class="mono">${esc(task.local)}</div><div class="b-endpoint-bottom"><span>${esc(task.files)} 个文件</span><span>${esc(task.size)}</span></div></article><div class="b-flow">${icon('arrow')}<span>单向</span><span>镜像</span></div><article class="b-endpoint"><div class="b-endpoint-header">${icon('phone')}手机 NAS<span class="endpoint-label">镜像 · 自动维护</span></div><div class="b-folder-glyph">${icon('folders')}</div><h3>当前镜像</h3><div class="mono">/${esc(task.remote)}</div><div class="b-endpoint-bottom"><span>vivo V2227A</span><span>${state.mounted?'X: 已挂载':'未挂载'} · 演示</span></div></article></section><section class="b-rules"><div class="b-rule"><span class="small">同步频率</span><strong>${icon('clock')}每 ${task.interval} 分钟</strong></div><div class="b-rule"><span class="small">旧文件保留</span><strong>${icon('history')}${task.retention} 天</strong></div><div class="b-rule"><span class="small">排除规则</span><strong>${icon('shield')}${task.preset==='recommended'?'跳过构建产物':task.preset==='none'?'不排除任何文件':'自定义排除'}</strong></div></section><section class="b-version-strip">${icon('history')}<div><h3>被覆盖或删除的文件，会先留一份。</h3><div class="mono">/${esc(task.versions)}/&lt;时间戳&gt;</div><p>这里保留的是变化前的旧文件，不是每次同步的完整快照。</p></div><button class="text-btn" data-action="versions" data-id="${task.id}">查看历史 ${icon('chevron')}</button></section><section><div class="b-log-head"><h3>这个文件夹的动态</h3><span class="small muted" style="font-size:10px">演示数据</span></div>${[{time:task.last,text:'镜像已是最新',detail:'本轮无变化',glyph:'check'},{time:'09:00',text:'自动检查文件差异',detail:'只传变化文件',glyph:'eye'},{time:'08:40',text:'旧文件已移入历史目录',detail:`保留 ${task.retention} 天`,glyph:'history'}].map(e=>`<div class="b-log-row"><span class="mono">${esc(e.time)}</span>${icon(e.glyph)}<span>${e.text}</span><span class="log-detail">${e.detail}</span></div>`).join('')}</section><div class="b-bottomline"><span>${icon('clock')}${task.enabled?'下次计划 '+task.next:'这个任务已暂停'}</span><div class="row">${button('先看差异','preview','subtle','eye',`data-id="${task.id}"`)}${button('编辑任务','edit','secondary','edit',`data-id="${task.id}"`)}</div></div>`;
}
function VariantC(){
  return `${winbar()}<div class="c-shell"><aside class="c-rail"><div class="brand-mark">${icon('network')}</div><nav aria-label="主导航">${navItems.map(([key,glyph,label])=>`<button class="icon-btn ${state.page===key?'active':''}" data-action="navigate" data-page="${key}" aria-label="${label}" title="${label}">${icon(glyph)}</button>`).join('')}</nav><div class="rail-bottom">${tinyButton('查看原型状态','state','code')}</div></aside><main class="c-main"><div class="c-topbar"><div class="wordmark">PhoneNAS <span>WORKSPACE / ${state.page==='tasks'?'SYNC JOBS':'LOCAL STORAGE'}</span></div><div class="row"><span class="mono">100.109.166.11:8888</span><span class="c-live"><i class="dot"></i>DEMO / ONLINE</span><div class="avatar">P</div></div></div>${state.page==='tasks'?tasksC():secondaryPage()}</main></div>`;
}
function tasksC(){
  return `${standardHead(`同步任务 <span>${String(state.tasks.length).padStart(2,'0')}</span>`,'一处管理所有文件夹。独立策略，串行队列，可控的每一次同步。',button('添加多个','batch','secondary','folders')+button('新建任务','add','primary','plus'))}<section class="c-metrics"><div class="c-metric"><div class="c-metric-top">${icon('link')}存储连接</div><div class="c-metric-value"><strong class="good-text">${state.mounted?'CONNECTED':'UNMOUNTED'}</strong></div></div><div class="c-metric"><div class="c-metric-top">${icon('folders')}启用任务</div><div class="c-metric-value"><strong>${String(state.tasks.filter(t=>t.enabled).length).padStart(2,'0')}</strong><small>/ ${state.tasks.length} TOTAL</small></div></div><div class="c-metric"><div class="c-metric-top">${icon('activity')}执行队列</div><div class="c-metric-value"><strong>${String(state.queue.length+(state.running?1:0)).padStart(2,'0')}</strong><small>${state.running?'1 RUNNING':'IDLE'}</small></div></div><div class="c-metric"><div class="c-metric-top">${icon('shield')}历史隔离</div><div class="c-metric-value"><strong class="good-text">ENABLED</strong><small>PER JOB</small></div></div></section><div class="c-table-tools">${state.selected.size?`<div class="c-toolbar-bulk">已选 ${state.selected.size} 项 ${button('同步所选','syncSelected','subtle','sync')}${button('暂停所选','pauseSelected','subtle','pause')}${tinyButton('取消选择','clearSelected','close')}</div>`:taskFilters('filter-tabs')}${searchBox()}</div><div class="c-table-wrap"><table class="c-table"><thead><tr><th><input type="checkbox" aria-label="选择全部任务" data-select-all ${state.tasks.length&&state.selected.size===state.tasks.length?'checked':''}></th><th>任务 / 本地来源</th><th>状态</th><th>镜像目录</th><th>计划</th><th>保留</th><th>上次成功</th><th>操作</th></tr></thead><tbody>${filteredTasks().map(task=>`<tr><td><input type="checkbox" aria-label="选择 ${esc(task.name)}" data-select-task="${task.id}" ${state.selected.has(task.id)?'checked':''}></td><td><div class="c-task-name">${icon('folder')}${esc(task.name)}${task.demo?'<span class="tag">示例</span>':''}</div><div class="mono">${esc(task.local)}</div></td><td>${taskStatus(task)}${state.running===task.id?`<div class="progress"><i style="width:${task.progress||0}%"></i></div>`:''}</td><td><span class="mono">${esc(task.remote)}</span></td><td>${task.interval} min</td><td>${task.retention} d</td><td class="mono">${esc(task.last)}</td><td><div class="row">${tinyButton('同步 '+task.name,'start','play',task.id)}${tinyButton('编辑 '+task.name,'edit','edit',task.id)}${tinyButton(task.enabled?'暂停 '+task.name:'继续 '+task.name,'toggle',task.enabled?'pause':'play',task.id)}</div></td></tr>`).join('')||'<tr><td colspan="8">没有匹配任务</td></tr>'}</tbody></table></div><div class="c-table-footer"><span>${state.tasks.length} 个任务 &nbsp; / &nbsp; 所有任务使用独立目录</span><div class="row"><span>${state.paused?'AUTOMATION PAUSED':'SERIAL MODE · MAX 1'}</span><button class="text-btn" data-action="pauseAll">${state.paused?'继续计划':'暂停计划'}</button><button class="text-btn" data-action="syncAll">${icon('sync')}同步全部</button></div></div><div class="c-bottom"><section class="c-bottom-panel"><div class="section-title"><h3>执行记录</h3><span class="eyebrow">SIMULATED EVENTS</span></div>${state.events.slice(0,3).map(e=>`<div class="c-event"><span class="mono">${esc(e.time)}</span><i class="dot"></i><span>${esc(e.task)} &nbsp;·&nbsp; ${esc(e.text)}</span></div>`).join('')}</section><section class="c-bottom-panel"><div class="section-title"><h3>连接与保护</h3><span class="eyebrow">STORAGE POLICY</span></div><div class="c-storage-line"><strong>vivo V2227A</strong><span class="mono">${state.mounted?'X: / MOUNTED':'UNMOUNTED'}</span></div><div class="c-storage-line"><span class="muted">Tailscale / WebDAV</span><span class="good-text" style="font-size:10px">演示连接</span></div><div class="c-policy">${icon('shield')}源目录异常或大量删除时应先预演。移除任务仅移除配置，不自动删除手机上的文件。</div></section></div>`;
}
function emptyTasks(){return `<div class="empty">${icon('folders')}<p>还没有匹配的文件夹</p><small>添加文件夹，或调整搜索和筛选条件。</small></div>`;}
function secondaryPage(){
  if(state.page==='versions') return versionsPage();
  if(state.page==='activity') return activityPage();
  if(state.page==='settings') return settingsPage();
  return overviewPage();
}
function overviewPage(){
  return `${standardHead('你的口袋 NAS。','连接状态、文件夹保护与最近动态，一眼看清。',button('管理同步任务','navigate','primary','folders','data-page="tasks"'))}<div class="generic-columns"><section class="generic-panel"><div class="section-title"><h3>手机存储</h3><span class="badge good"><i class="dot"></i>在线 · 演示</span></div><div class="a-nas-illustration" aria-hidden="true"><div class="phone-drawing">${icon('shield')}</div><div class="connection-dots"><i></i><i></i><i></i></div><div class="disk-drawing">${icon('disk')}</div></div><div class="generic-line"><span>设备</span><strong>vivo V2227A</strong></div><div class="generic-line"><span>WebDAV 地址</span><span class="mono">http://100.109.166.11:8888</span></div><div class="generic-line"><span>网络盘 ${state.mounted?'X:':'未挂载'}</span>${button(state.mounted?'卸载盘符':'挂载盘符','mount','secondary','disk')}</div><p class="prototype-strip">${icon('info')}不控制手机服务；这里仅演示电脑端挂载与同步。</p></section><section class="generic-panel"><div class="section-title"><h3>文件夹保护</h3><span class="tag">单向镜像 + 旧文件历史</span></div><div class="summary-big">${state.tasks.filter(t=>t.enabled).length} <small class="muted" style="font-size:14px;letter-spacing:0">/ ${state.tasks.length} 个文件夹已启用</small></div><p class="version-description">每个文件夹拥有自己的目标目录、同步频率、排除规则和保留时间。</p>${state.tasks.map(t=>`<div class="generic-line"><div><h3 style="font-size:12px">${esc(t.name)}</h3><p>每 ${t.interval} 分钟 · 历史保留 ${t.retention} 天</p></div>${taskStatus(t)}</div>`).join('')}<div class="row" style="margin-top:18px">${button('同步全部','syncAll','primary','sync')}${button('添加文件夹','add','secondary','plus')}</div></section></div>`;
}
function versionsPage(){
  const task=taskById(state.versionTask)||state.tasks[0];
  if(!task) return emptyTasks();
  const dates=[['2026-11-14 09:20','2 个旧文件 · 1.4 MiB'],['2026-11-13 18:40','3 个旧文件 · 3.8 MiB'],['2026-11-12 10:00','1 个旧文件 · 824 KiB']];
  return `${standardHead('变化前的文件，都有迹可循。','按文件夹查看被覆盖、删除的旧文件。不是完整时间点快照。',`<select class="task-select" aria-label="查看哪个任务的历史" data-version-select>${state.tasks.map(t=>`<option value="${t.id}" ${t.id===task.id?'selected':''}>${esc(t.name)}</option>`).join('')}</select>`)}<div class="notice">${icon('info')}一个时间戳目录只包含该轮变动前的旧文件。「恢复这批旧文件」不会把整个文件夹还原到当时的完整状态。</div><div class="versions-layout"><div>${dates.map((d,i)=>`<button class="version-date ${state.versionIndex===i?'active':''}" data-action="versionDate" data-index="${i}"><strong>${d[0]}</strong><small>${d[1]}</small></button>`).join('')}</div><section class="generic-panel"><div class="section-title"><h3>${dates[state.versionIndex][0]}</h3>${button('恢复这批旧文件','restore','secondary','restore')}</div><p class="version-description">${esc(task.name)} · 历史保留 ${task.retention} 天 · 下列内容为示例</p><div class="mono muted" style="font-size:10px;margin-top:8px;overflow-wrap:anywhere">/${esc(task.versions)}/2026-11-14_092000/</div><table class="file-list"><thead><tr><th>旧文件</th><th>大小</th><th>保留原因</th><th></th></tr></thead><tbody><tr><td><span class="row">${icon('file')}项目说明.md</span></td><td class="mono">12 KiB</td><td><span class="badge blue">被覆盖</span></td><td>${button('恢复','restore','subtle','restore')}</td></tr><tr><td><span class="row">${icon('file')}旧版需求.docx</span></td><td class="mono">1.4 MiB</td><td><span class="badge neutral">被删除</span></td><td>${button('恢复','restore','subtle','restore')}</td></tr></tbody></table><p class="prototype-strip">${icon('shield')}正式版默认恢复到新目录；覆盖原文件前需明确确认。</p></section></div>`;
}
function activityPage(){
  return `${standardHead('每一次变动，清晰可见。','任务执行、历史保留与连接事件分开记录。',button('返回同步任务','navigate','secondary','folders','data-page="tasks"'))}<section class="generic-panel"><div class="section-title"><h3>今天</h3><span class="tag">演示事件 · 不是真实执行日志</span></div>${state.events.map(e=>`<div class="activity-line"><span class="mono">${esc(e.time)}</span>${icon(e.kind)}<div>${esc(e.task)} · ${esc(e.text)}<p>${esc(e.detail)}</p></div><span class="badge good">已完成</span></div>`).join('')}</section>`;
}
function settingsPage(){
  return `${standardHead('简单的设置，清楚的边界。','连接与挂载是全局设置；同步策略属于每个文件夹。',button('查看任务设置','navigate','primary','folders','data-page="tasks"'))}<div class="setting-grid"><section class="generic-panel"><div class="section-title"><h3>NAS 连接</h3><span class="badge good">共享连接 · 演示</span></div><div class="static-field"><label>WebDAV 地址</label><div class="mock-input mono">http://100.109.166.11:8888</div></div><div class="static-field"><label>连接凭据</label><div class="mock-input">原型不读取或保存账号密码</div></div>${button('测试演示连接','testConnection','secondary','link')}<div class="generic-line" style="margin-top:15px"><div><span>自动挂载 X:</span><p>所有同步任务共用一个网络盘</p></div><button data-action="mount" aria-label="切换演示挂载"><span class="toggle ${state.mounted?'on':''}"></span></button></div></section><section class="generic-panel"><div class="section-title"><h3>任务级设置</h3><span class="tag">多个文件夹，分别配置</span></div>${state.tasks.map(task=>`<div class="generic-line"><div>${esc(task.name)}<p>${task.interval} 分钟 · 保留 ${task.retention} 天</p></div>${button('编辑','edit','secondary','edit',`data-id="${task.id}"`)}</div>`).join('')}<div class="notice" style="margin-top:18px">${icon('info')}旧版本的单文件夹配置会迁移成第一个任务，保留原远端路径和历史数据，不重新上传。</div></section></div>`;
}
function renderSwitcher(){
  document.getElementById('switcher').innerHTML=`<aside class="prototype-switcher" aria-label="原型方案切换"><div class="switcher-note"><strong>UI 原型 · 非正式版</strong><small>模拟数据 · 不写入文件</small></div><button class="switcher-arrow" data-action="previous" aria-label="上一个原型">‹</button><div class="variant-picks">${Object.entries(variants).map(([key,v])=>`<button data-action="variant" data-variant="${key}" class="${state.variant===key?'active':''}" aria-pressed="${state.variant===key}" title="${v.detail}"><b>${key}</b>${v.name}</button>`).join('')}</div><button class="switcher-arrow" data-action="next" aria-label="下一个原型">›</button><div class="switcher-extra">${tinyButton('查看原型状态','state','code')}${tinyButton('原型使用说明','help','help')}</div></aside>`;
}
function switchVariant(key){
  state.variant=key;
  const url=new URL(location.href);url.searchParams.set('variant',key);history.replaceState(null,'',url);
  render();
  console.info('PhoneNAS prototype: full relevant state',getRelevantState());
}
function cycleVariant(step){const keys=Object.keys(variants);switchVariant(keys[(keys.indexOf(state.variant)+step+keys.length)%keys.length]);}
function toast(message){
  const el=document.getElementById('toast');el.textContent=message;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),4000);
}
function modal(title, subtitle, content, footer, width=720){
  document.getElementById('overlay').innerHTML=`<div class="overlay-backdrop"><section class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}" style="width:${width}px"><header class="modal-head"><div><h2>${title}</h2><p>${subtitle}</p></div>${tinyButton('关闭弹窗','closeModal','close')}</header><div class="modal-content">${content}</div><footer class="modal-foot">${footer}</footer></section></div>`;
}
function closeModal(){document.getElementById('overlay').innerHTML='';editor=null;}
function slug(text){
  const safe=text.toLowerCase().replace(/[^a-z0-9-]/g,'-').replace(/-+/g,'-').replace(/^-|-$/g,'');
  return safe || `folder-${state.tasks.length+1}`;
}
function newTaskDraft(){
  const id=`folder-${Date.now().toString(36)}`;
  return {id,name:'',local:'',remote:`backup/${id}/mirror`,versions:`backup/${id}/versions`,interval:20,retention:90,preset:'recommended',enabled:true,size:'尚未扫描',files:'—',last:'从未执行',next:'待首次同步',tone:'',demo:true};
}
function openEditor(id){
  const task=taskById(id);
  editor={isNew:!task,tab:'basic',draft:task?structuredClone(task):newTaskDraft()};
  renderEditor();
}
function captureEditor(){
  const form=document.getElementById('task-form');if(!form||!editor)return;
  const data=new FormData(form);
  for(const key of ['name','local','remote','versions','interval','retention','preset','excludes']){
    if(data.has(key)) editor.draft[key]=['interval','retention'].includes(key)?Number(data.get(key)):String(data.get(key)).trim();
  }
  if(form.elements.namedItem('enabled'))editor.draft.enabled=data.has('enabled');
}
function renderEditor(){
  const {draft:d,isNew,tab}=editor;
  let content='';
  if(tab==='basic')content=`<div class="form-grid"><div class="field full"><label for="task-name">任务名称</label><input id="task-name" name="name" value="${esc(d.name)}" placeholder="例如：工作文件、产品文档"></div><div class="field full"><label for="task-local">电脑上的来源文件夹 <span style="color:#a1aec0;font-weight:400">只读，不改动源文件</span></label><input id="task-local" name="local" value="${esc(d.local)}" placeholder="D:\\需要备份的文件夹"><p>网页原型不打开真实文件选择器。可输入路径或点选下面的示例。</p><div class="demo-choices"><button type="button" data-action="sampleSource" data-sample="notes">${icon('folder')}个人笔记</button><button type="button" data-action="sampleSource" data-sample="photos">${icon('folder')}照片归档</button><button type="button" data-action="batch">${icon('folders')}批量添加…</button></div></div><div class="field full"><label for="task-remote">手机上的镜像目录</label><input id="task-remote" name="remote" value="${esc(d.remote)}"><p>每个任务使用独立的子目录，禁止与其他任务的镜像 / 历史目录重叠。</p></div></div><details><summary class="advanced-summary">高级 · 旧文件历史保存位置</summary><div class="field advanced-content"><input aria-label="历史目录" name="versions" value="${esc(d.versions)}"><p>当前镜像和历史目录必须互相隔离。</p></div></details><label class="form-checkbox"><input type="checkbox" name="enabled" ${d.enabled?'checked':''}>保存后启用此任务</label>`;
  if(tab==='schedule')content=`<div class="form-grid"><div class="field"><label for="task-interval">同步间隔</label><div class="unit-input"><input type="number" id="task-interval" name="interval" value="${d.interval}" min="5" max="1440"><span>分钟 / 次</span></div><p>5–1440 分钟，所有任务按队列串行执行。</p></div><div class="field"><label for="task-retention">旧文件保留</label><div class="unit-input"><input type="number" id="task-retention" name="retention" value="${d.retention}" min="1" max="3650"><span>天</span></div><p>只清理本任务的过期历史，不影响其他任务。</p></div><div class="field full"><div class="modal-notice">${icon('clock')}每个文件夹都有自己的计划。任务撞点时自动排队；一个任务失败，不阻塞后面的文件夹。电脑休眠错过的计划，在恢复后补一次，不累计重复执行。</div></div></div>`;
  if(tab==='rules')content=`<div class="form-grid"><div class="field full"><label for="task-preset">排除规则</label><select id="task-preset" name="preset" data-preset-select><option value="recommended" ${d.preset==='recommended'?'selected':''}>推荐 · 排除构建产物，保留 Release</option><option value="none" ${d.preset==='none'?'selected':''}>不排除 · 同步所有文件</option><option value="custom" ${d.preset==='custom'?'selected':''}>自定义规则</option></select><p>不再用一大块规则文本占满主界面。常用设置只需选择预设。</p></div><div class="field full">${d.preset==='recommended'?`<div class="rule-chips">${['node_modules','build / obj','.gradle','__pycache__','临时文件','系统缩略图'].map(v=>`<span class="rule-chip">${v}</span>`).join('')}</div><p style="margin-top:12px">Release 目录不被整目录排除；匹配这些规则的构建缓存会跳过。</p><details style="margin-top:15px"><summary class="advanced-summary">查看具体匹配规则</summary><div class="advanced-content"><textarea readonly aria-label="推荐排除规则">node_modules/**\n**/node_modules/**\nbuild/**\n**/build/**\nobj/**\n**/obj/**\n.gradle/**\n**/.gradle/**\n__pycache__/**\n**/__pycache__/**\n*.tmp\n~$*\nThumbs.db\ndesktop.ini</textarea></div></details>`:d.preset==='none'?'<div class="modal-notice">'+icon('info')+'所有文件都会参与同步，适合文档、照片等非构建目录。首次同步可能更大。</div>':`<label for="task-excludes">每行一个 rclone 匹配规则</label><textarea id="task-excludes" name="excludes" rows="7" placeholder="例如：**/node_modules/**">${esc(d.excludes||'')}</textarea>`}</div></div>`;
  modal(isNew?'添加一个同步文件夹':'编辑同步任务',isNew?'从一个文件夹开始。每个任务都有独立的镜像和历史。':`${esc(d.name)} · 修改仅在当前原型内生效`,`<div class="modal-tabs" role="tablist" aria-label="任务设置分组">${[['basic','文件夹'],['schedule','计划与保留'],['rules','排除规则']].map(([key,label])=>`<button role="tab" aria-selected="${tab===key}" class="${tab===key?'active':''}" data-action="editorTab" data-tab="${key}">${label}</button>`).join('')}</div><div id="form-error" class="form-error" role="alert"></div><form id="task-form">${content}</form><div class="modal-notice">${icon('shield')}演示模式：保存只修改页面里的模拟任务，不会读取电脑文件，也不会写入手机。</div>`,`<small>${isNew?'先建立任务，再预演或手动同步。':'移除任务不会删除手机中的镜像和历史文件。'}</small><div class="row">${!isNew?button('移除任务','remove','danger','trash',`data-id="${d.id}"`):''}${button('取消','closeModal','secondary')}${button(isNew?'添加文件夹':'保存修改','saveTask','primary','check')}</div>`);
}
function normalizeRemote(path){return String(path).replace(/\\/g,'/').replace(/\/+$/,'');}
function overlaps(a,b){return a===b||a.startsWith(b+'/')||b.startsWith(a+'/');}
function validateTask(d){
  if(!d.name||!d.local)return '请填写任务名称和来源文件夹，不能把空路径作为同步来源。';
  if(!/^[a-z]:[\\/]/i.test(d.local)&&!/^\\\\[^\\]+\\[^\\]+/.test(d.local))return '来源必须是绝对路径，例如 D:\\工作文件。';
  if(/^x:[\\/]/i.test(d.local))return '不要把 PhoneNAS 挂载的 X: 网络盘当作同步来源。';
  if(!Number.isInteger(d.interval)||d.interval<5||d.interval>1440)return '同步间隔应为 5–1440 分钟。请到“计划与保留”调整。';
  if(!Number.isInteger(d.retention)||d.retention<1||d.retention>3650)return '保留时长应为 1–3650 天。';
  d.remote=normalizeRemote(d.remote);d.versions=normalizeRemote(d.versions);
  if([d.remote,d.versions].some(p=>!p||p.startsWith('/')||p.includes(':')||p.split('/').some(s=>!s||s==='..'||s==='.')))return '远端目录应为 NAS 根目录下的相对子目录，不能是根目录或含有 ..。';
  if(overlaps(d.remote,d.versions))return '这个任务的镜像和历史目录不能相同，也不能互相包含。';
  for(const t of state.tasks.filter(t=>t.id!==d.id)){
    if(d.local.replace(/\\+$/,'').toLowerCase()===t.local.replace(/\\+$/,'').toLowerCase())return `“${t.name}”已经使用这个来源文件夹。`;
    if([d.remote,d.versions].some(a=>[t.remote,t.versions].some(b=>overlaps(a,b))))return `远端路径与“${t.name}”重叠；不同任务必须使用独立目录，避免互相覆盖或删除。`;
  }
  return '';
}
function saveTask(){
  captureEditor();const error=validateTask(editor.draft);
  if(error){const el=document.getElementById('form-error');el.textContent=error;el.classList.add('visible');return;}
  const d=structuredClone(editor.draft);const i=state.tasks.findIndex(t=>t.id===d.id);
  if(i<0)state.tasks.push(d);else state.tasks[i]=d;
  state.selectedTask=d.id;closeModal();render();toast('已保存到当前原型。没有创建真实同步任务。');
}
function openBatch(){
  editor=null;
  modal('一次添加多个文件夹','正式版使用文件夹选择器；这里用示例文件夹演示批量添加。',`<p class="state-description">每个文件夹会创建一个独立任务。添加后可分别设置频率、保留天数和排除规则。</p><form id="batch-form">${[['notes','个人笔记','D:\\PersonalNotes'],['photos','照片归档','E:\\PhotoArchive'],['archive','项目归档','D:\\ProjectArchive']].map(([key,name,local])=>`<label class="generic-line" style="cursor:pointer"><span class="row"><input type="checkbox" name="folders" value="${key}" ${key!=='archive'?'checked':''} style="accent-color:#3768e7">${icon('folder')}<span>${name}<p class="mono">${local}</p></span></span><span class="badge neutral">示例</span></label>`).join('')}</form><div class="modal-notice">${icon('shield')}只添加内存中的示例任务，不访问真实文件夹。每个任务自动分配独立远端目录。</div>`,`<small>默认 20 分钟 / 保留 90 天，可逐项修改。</small><div class="row">${button('取消','closeModal','secondary')}${button('添加所选文件夹','saveBatch','primary','plus')}</div>`,640);
}
function saveBatch(){
  const values=new FormData(document.getElementById('batch-form')).getAll('folders');
  if(!values.length){toast('请至少勾选一个示例文件夹。');return;}
  const samples={notes:['个人笔记','D:\\PersonalNotes','personal-notes'],photos:['照片归档','E:\\PhotoArchive','photo-archive'],archive:['项目归档','D:\\ProjectArchive','project-archive']};
  let count=0;
  for(const key of values){const [name,local,slug]=samples[key];if(state.tasks.some(t=>t.local===local))continue;const d=newTaskDraft();Object.assign(d,{name,local,remote:`backup/${slug}/mirror`,versions:`backup/${slug}/versions`,preset:'none'});state.tasks.push(d);count++;}
  closeModal();render();toast(count?`已添加 ${count} 个示例任务，各自拥有独立配置。`:'这些示例文件夹已添加，不重复创建。');
}
function openPreview(id){
  const task=taskById(id);if(!task)return;
  modal('先看看，这次会发生什么。',`${esc(task.name)} · 差异预演（以下是模拟结果）`,`<div class="state-description"><strong>${esc(task.local)}</strong><br>单向镜像到 <span class="mono">/${esc(task.remote)}</span></div><div class="notice">${icon('shield')}预演不改动任何文件。正式版会先扫描，再显示新增、更新、删除的数量与大小。</div><table class="preview-table"><thead><tr><th>变动</th><th>文件</th><th>手机上的处理方式</th></tr></thead><tbody><tr><td><span class="badge good">新增</span></td><td class="mono">docs/项目计划.md</td><td>复制到当前镜像</td></tr><tr><td><span class="badge blue">更新</span></td><td class="mono">README.md</td><td>旧文件先移入历史</td></tr><tr><td><span class="badge amber">删除</span></td><td class="mono">docs/旧版说明.md</td><td>移入历史，保留 ${task.retention} 天</td></tr></tbody></table><p class="state-description" style="font-size:10px;margin:17px 0 0">正式版将对源目录缺失、源异常变空、大量删除进行额外保护。</p>`,`<small>仅模拟 3 项变化 · 未扫描真实文件</small><div class="row">${button('关闭','closeModal','secondary')}${button('模拟执行','previewRun','primary','play',`data-id="${task.id}"`)}</div>`);
}
function enqueue(ids){
  const valid=ids.filter(id=>taskById(id)?.enabled);
  if(!valid.length){toast('任务已暂停。先启用任务，再执行演示同步。');return;}
  for(const id of valid){if(id!==state.running&&!state.queue.includes(id))state.queue.push(id);}
  toast('模拟任务已入队；不会访问手机或传输真实文件。');
  dispatch();render();
}
function dispatch(){
  if(state.running||!state.queue.length)return;
  const id=state.queue.shift(),task=taskById(id);
  if(!task?.enabled){dispatch();return;}
  state.running=id;task.progress=0;
  const timer=setInterval(()=>{
    const live=taskById(id);if(!live){clearInterval(timer);state.running=null;dispatch();render();return;}
    live.progress=Math.min(100,(live.progress||0)+20);
    if(live.progress>=100){
      clearInterval(timer);live.last=timeNow().slice(0,5);live.next='按计划执行';delete live.progress;
      state.events.unshift({time:timeNow(),task:live.name,text:'模拟同步完成',detail:'仅更新原型状态 · 未传输文件',kind:'check'});
      state.running=null;dispatch();
    }
    render();
  },450);
}
function openState(){
  modal('原型状态','所有内容只保存在当前页面内存中，刷新即重置。',`<p class="state-description">当前方案：<strong>${state.variant} · ${variants[state.variant].name}</strong><br>${variants[state.variant].detail}<br>任务数 ${state.tasks.length} · 当前页面 ${state.page} · 无真实后端</p><pre class="state-json">${esc(JSON.stringify(getRelevantState(),null,2))}</pre>`,`<small>切换方案保留同一组模拟任务，便于比较。</small>${button('明白了','closeModal','primary')}`);
}
function openHelp(){
  modal('这三个原型，怎么看？','先比较结构，再决定哪种体验更适合。',`<div class="state-description"><p><strong>A · 清爽工作台（推荐）</strong><br>任务卡片集中展示状态，右侧保留设备与活动。适合日常使用。</p><br><p><strong>B · 文件夹映射</strong><br>左侧选文件夹，右侧明确展示“电脑 → 手机”和版本去向。适合理解每个任务。</p><br><p><strong>C · 紧凑控制台</strong><br>表格批量管理，显示策略与执行队列。适合很多文件夹。</p><br><p>可以添加、批量添加、编辑、暂停、预演、模拟同步，以及按任务浏览历史。所有操作只改变内存，不会连接手机或改动已有应用。</p><br><p>用底部按钮或键盘 ← / → 切换方案；URL 中的 <span class="mono">?variant=A|B|C</span> 可直接打开指定方案。</p></div>`,`<small>界面原型 ≠ 已完成的桌面功能。</small>${button('开始比较','closeModal','primary')}`,640);
}
function removeTask(id){
  const task=taskById(id);if(!task)return;
  if(state.running===id){toast('请等这个模拟任务结束，再移除。');return;}
  editor=null;
  modal('只移除任务，不删除文件。',esc(task.name),`<p class="state-description">移除后，不再自动同步这个文件夹。<br>正式版也会默认保留手机上的当前镜像与历史文件。</p><div class="modal-notice">${icon('shield')}本次仅从原型内存里移除示例，不修改真实配置。</div>`,`<small>数据删除应是单独、明确的操作。</small><div class="row">${button('取消','closeModal','secondary')}${button('移除演示任务','confirmRemove','danger','trash',`data-id="${id}"`)}</div>`,540);
}

document.addEventListener('click',event=>{
  const target=event.target.closest('[data-action]');if(!target)return;
  const {action,id}=target.dataset;
  if(action==='variant')return switchVariant(target.dataset.variant);
  if(action==='previous')return cycleVariant(-1);
  if(action==='next')return cycleVariant(1);
  if(action==='navigate'){state.page=target.dataset.page;render();return;}
  if(action==='selectTask'){state.selectedTask=id;state.page='tasks';render();return;}
  if(action==='filter'){state.filter=target.dataset.filter;render();return;}
  if(action==='add')return openEditor();
  if(action==='edit')return openEditor(id);
  if(action==='editorTab'){captureEditor();editor.tab=target.dataset.tab;renderEditor();return;}
  if(action==='saveTask')return saveTask();
  if(action==='sampleSource'){
    captureEditor();const name=target.dataset.sample==='notes'?'个人笔记':'照片归档';const local=target.dataset.sample==='notes'?'D:\\PersonalNotes':'E:\\PhotoArchive';const key=target.dataset.sample==='notes'?'personal-notes':'photo-archive';
    Object.assign(editor.draft,{name,local,remote:`backup/${key}/mirror`,versions:`backup/${key}/versions`});renderEditor();return;
  }
  if(action==='batch')return openBatch();
  if(action==='saveBatch')return saveBatch();
  if(action==='closeModal')return closeModal();
  if(action==='toggle'){
    const task=taskById(id);if(!task)return;
    task.enabled=!task.enabled;
    if(!task.enabled)state.queue=state.queue.filter(q=>q!==id);
    render();toast(task.enabled?'已启用这个示例任务。':'已暂停这个示例任务；正在执行的一轮会正常结束。');return;
  }
  if(action==='pauseAll'){state.paused=!state.paused;render();toast(state.paused?'已暂停自动计划，手动同步仍可使用。':'已恢复演示计划。原型不会自动执行真实任务。');return;}
  if(action==='start')return enqueue([id]);
  if(action==='syncAll')return enqueue(state.tasks.map(t=>t.id));
  if(action==='syncSelected')return enqueue([...state.selected]);
  if(action==='pauseSelected'){for(const id of state.selected){const t=taskById(id);if(t)t.enabled=false;}state.queue=state.queue.filter(id=>!state.selected.has(id));state.selected.clear();render();toast('选中的示例任务已暂停。');return;}
  if(action==='clearSelected'){state.selected.clear();render();return;}
  if(action==='preview')return openPreview(id);
  if(action==='previewRun'){closeModal();return enqueue([id]);}
  if(action==='versions'){state.versionTask=id;state.versionIndex=0;state.page='versions';render();return;}
  if(action==='versionDate'){state.versionIndex=Number(target.dataset.index);render();return;}
  if(action==='restore')return modal('把旧文件恢复到新位置','恢复流程原型 · 不复制真实文件',`<p class="state-description">正式版会让你选择恢复目录，并显示将要恢复的文件。默认使用新目录，避免覆盖当前工作文件。</p><div class="field"><label>示例恢复位置</label><input value="D:\\PhoneNAS-Restore\\2026-11-14" readonly></div><div class="modal-notice">${icon('info')}这里恢复的是选中的旧文件，不是完整时间点快照。</div>`,`<small>无真实文件操作</small>${button('完成演示','restoreDone','primary','check')}`,580);
  if(action==='restoreDone'){closeModal();toast('恢复流程演示结束。没有写入任何文件。');return;}
  if(action==='mount'){state.mounted=!state.mounted;render();toast(state.mounted?'演示盘符已挂载（不会创建真实 X: 盘）。':'演示盘符已卸载（不影响电脑上的真实挂载）。');return;}
  if(action==='openDrive'){toast('正式版会打开 X: 网络盘。原型不打开真实文件或调用系统程序。');return;}
  if(action==='testConnection'){toast('演示连接正常。未发送网络请求，未读取真实凭据。');return;}
  if(action==='remove')return removeTask(id);
  if(action==='confirmRemove'){state.tasks=state.tasks.filter(t=>t.id!==id);state.queue=state.queue.filter(t=>t!==id);state.selected.delete(id);closeModal();render();toast('已移除示例任务，真实文件和配置不受影响。');return;}
  if(action==='state')return openState();
  if(action==='help')return openHelp();
});
document.addEventListener('input',event=>{
  if(!event.target.matches('[data-search]'))return;
  const value=event.target.value;const caret=event.target.selectionStart;state.query=value;render();
  const next=document.querySelector('[data-search]');next?.focus();next?.setSelectionRange(caret,caret);
});
document.addEventListener('change',event=>{
  const target=event.target;
  if(target.matches('[data-version-select]')){state.versionTask=target.value;state.versionIndex=0;render();}
  if(target.matches('[data-preset-select]')){captureEditor();renderEditor();}
  if(target.matches('[data-select-task]')){const id=target.dataset.selectTask;target.checked?state.selected.add(id):state.selected.delete(id);render();}
  if(target.matches('[data-select-all]')){state.selected=target.checked?new Set(state.tasks.map(t=>t.id)):new Set();render();}
});
document.addEventListener('submit',event=>{event.preventDefault();if(event.target.id==='task-form')saveTask();});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'){closeModal();return;}
  if(document.getElementById('overlay').children.length||event.target.closest('input, textarea, select, [contenteditable]'))return;
  if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();cycleVariant(event.key==='ArrowLeft'?-1:1);}
});
window.addEventListener('popstate',()=>{const key=new URL(location.href).searchParams.get('variant')?.toUpperCase();if(variants[key]){state.variant=key;render();}});
render();
console.info('PhoneNAS prototype: full relevant state',getRelevantState());
