
let sess=null,busy=false,cnt=0,bidx=0,showI=false,showTests=false;
let currentLang='cn';
const I18N={cn:{title:'智能客服',name:'智能客服助手',status:'在线 · 通常秒回',placeholder:'按 Enter 发送 · Ctrl+Enter 换行',newSess:'新会话已启动',welcome:'👋 您好！我是智能客服助手。\n\n可以帮您：\n• 📦 产品咨询与使用指导\n• 🔧 故障排查与技术支持\n• 💰 价格与保修政策\n• 📞 投诉与建议\n\n请问有什么可以帮您的？',qrDef:['产品怎么用？','我要投诉','价格是多少？','有保修吗？'],qrReply:['能详细说说吗？','还有其他问题','谢谢，没问题了'],qrSat:['满意','不满意'],copy:'复制',copied:'✓',analyzing:'🤔 分析中...',helpful:'有帮助？',thanks:'感谢评价！',darkBtn:'🌙 暗色',lightBtn:'☀️ 亮色',langBtnCN:'🇨🇳 CN',langBtnEN:'🇬🇧 EN'},en:{title:'Smart Assistant',name:'Customer Service Bot',status:'Online · Usually instant reply',placeholder:'Press Enter to send · Ctrl+Enter for newline',newSess:'New session started',welcome:'👋 Hello! I\'m your customer service assistant.\n\nI can help you with:\n• 📦 Product inquiries & usage guides\n• 🔧 Troubleshooting & tech support\n• 💰 Pricing & warranty policies\n• 📞 Complaints & feedback\n\nHow can I assist you today?',qrDef:['How to use the product?','I want to complain','What\'s the price?','Is there warranty?'],qrReply:['Can you elaborate?','Any other questions?','Thanks, all good!'],qrSat:['Satisfied','Unsatisfied'],copy:'Copy',copied:'✓',analyzing:'🤔 Analyzing...',helpful:'Helpful?',thanks:'Thanks for rating!',darkBtn:'🌙 Dark',lightBtn:'☀️ Light',langBtnCN:'🇨🇳 CN',langBtnEN:'🇬🇧 EN'}};
function t(k){return I18N[currentLang][k]||k}
const QR={def:I18N.cn.qrDef,reply:I18N.cn.qrReply,sat:I18N.cn.qrSat};
function updateQR(){QR.def=I18N[currentLang].qrDef;QR.reply=I18N[currentLang].qrReply;QR.sat=I18N[currentLang].qrSat}

// ── Toolbar ────────────────────────────────────────────────────
function toggleToolbar(){const m=document.getElementById('toolbarMenu'),b=document.getElementById('toolbarBtn');m.classList.toggle('show');b.classList.toggle('active')}
function closeToolbar(){document.getElementById('toolbarMenu').classList.remove('show');document.getElementById('toolbarBtn').classList.remove('active')}
document.addEventListener('click',e=>{const t=document.querySelector('.toolbar');if(t&&!t.contains(e.target))closeToolbar()})

function toggleTests(){showTests=!showTests;document.getElementById('testPanel').classList.toggle('show',showTests)}
function toggleLang(){currentLang=currentLang==='cn'?'en':'cn';applyLang();localStorage.setItem('lang',currentLang)}
function applyLang(){const lt=I18N[currentLang];document.title=lt.title;document.querySelector('.c-header .name').textContent=lt.name;document.querySelector('.c-header .st').textContent=lt.status;inp.placeholder=lt.placeholder;document.getElementById('langIcon').textContent=currentLang==='cn'?'🇨🇳':'🇬🇧';document.getElementById('langLabel').textContent=currentLang==='cn'?'中文':'English';updateQR()}
if(localStorage.getItem('lang')){currentLang=localStorage.getItem('lang');setTimeout(applyLang,0)}
function toggleTheme(){const h=document.documentElement;if(h.getAttribute('data-theme')==='dark'){h.removeAttribute('data-theme');document.getElementById('themeIcon').textContent='🌙';document.getElementById('themeLabel').textContent='暗色模式';localStorage.setItem('th','light')}else{h.setAttribute('data-theme','dark');document.getElementById('themeIcon').textContent='☀️';document.getElementById('themeLabel').textContent='亮色模式';localStorage.setItem('th','dark')}}
if(localStorage.getItem('th')==='dark'){document.documentElement.setAttribute('data-theme','dark');setTimeout(()=>{document.getElementById('themeIcon').textContent='☀️';document.getElementById('themeLabel').textContent='亮色模式'},0)}
function toggleInfo(){showI=!showI;document.getElementById('infoBar').style.display=showI?'flex':'none'}
const chat=document.getElementById('chat'),inp=document.getElementById('inp'),sbtn=document.getElementById('sbtn');
inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.ctrlKey&&!busy)sendMsg();else if(e.key==='Enter'&&e.ctrlKey){e.preventDefault();inp.value+='\n';updateCharCount()}});
function updateCharCount(){const c=document.getElementById('charCount');if(!c)return;const n=inp.value.length;c.textContent=n+'/4000';c.classList.toggle('warn',n>3500)}
inp.addEventListener('input',updateCharCount);

function md(text){
  if(!text)return text;
  let s=text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  s=s.replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>');
  s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
  s=s.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/__(.+?)__/g,'<strong>$1</strong>');
  s=s.replace(/\*(.+?)\*/g,'<em>$1</em>');
  s=s.replace(/(?<!\w)_(.+?)_(?!\w)/g,'<em>$1</em>');
  s=s.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  s=s.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  s=s.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  s=s.replace(/^(?:[-*])\s+(.+)$/gm,'<li>$1</li>');
  s=s.replace(/((?:<li>.*<\/li>\n?)+)/g,'<ul>$1</ul>');
  s=s.replace(/^---$/gm,'<hr>');
  s=s.replace(/\n\n/g,'<br><br>');
  return s;
}

function hideEmptyState(){const e=document.getElementById('emptyState');if(e)e.style.display='none'}
let msgUid=0;
function addMsg(role,text,type,anim){hideEmptyState();cnt++;msgUid++;if(showI)document.getElementById('iMsg').textContent=cnt;const row=document.createElement('div');row.className='msg '+role;if(role==='bot')row.id='bot-'+msgUid;const av=document.createElement('div');av.className='av';av.textContent=role==='user'?'你':'🤖';const body=document.createElement('div');body.className='body';const bub=document.createElement('div');bub.className='bub '+(type||'');const meta=document.createElement('div');meta.className='meta';const ts=document.createElement('span');ts.textContent=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});meta.appendChild(ts);if(role==='bot'&&text){const rt=document.createElement('span');rt.textContent=/[\u4e00-\u9fff]/.test(text)?Math.ceil(text.length/5)+'秒':Math.ceil(text.split(/\s+/).filter(w=>w).length/2.7)+'s';meta.appendChild(rt);const cp=document.createElement('button');cp.className='cp';cp.textContent='复制';cp.onclick=()=>{navigator.clipboard.writeText(bub.dataset.raw||bub.textContent).then(()=>{cp.textContent='✓';setTimeout(()=>cp.textContent='复制',1500)})};meta.appendChild(cp)}body.appendChild(bub);body.appendChild(meta);row.appendChild(av);row.appendChild(body);chat.appendChild(row);chat.scrollTop=chat.scrollHeight;rmQR();if(anim&&role==='bot'){const spd=/[\u4e00-\u9fff]/.test(text)?35:20;bub.classList.add('cur');bub.dataset.raw=text;let i=0;(function t(){if(i<text.length){bub.innerHTML=md(text.substring(0,i+1));i++;chat.scrollTop=chat.scrollHeight;setTimeout(t,spd)}else bub.classList.remove('cur')})()}else{if(role==='bot'){bub.dataset.raw=text;bub.innerHTML=md(text)}else bub.textContent=text}if(role==='bot'&&type!=='satisfaction'&&type!=='closing'){bidx++;addStars(body,bidx);addReactions(body,'bot-'+msgUid);if(text)addSpeakBtn(body,text)}return row}
function addReactions(parent,msgId){const d=document.createElement('div');d.className='reactions';const emojis=['👍','👎','💡'];emojis.forEach(e=>{const b=document.createElement('button');b.textContent=e;b.title=e==='👍'?'有帮助':e==='👎'?'没帮助':'有启发';b.onclick=()=>{b.classList.toggle('active');fetch('/api/reaction',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sess,message_id:msgId,emoji:e,active:b.classList.contains('active')})}).catch(()=>{})};d.appendChild(b)});parent.appendChild(d)}
function addSys(t){const d=document.createElement('div');d.className='sys';d.textContent=t;chat.appendChild(d);chat.scrollTop=chat.scrollHeight}
function addTyping(){const row=document.createElement('div');row.className='msg bot';row.id='typing';row.innerHTML='<div class="av">🤖</div><div class="body"><div class="bub"><div class="typing"><span></span><span></span><span></span></div></div></div>';chat.appendChild(row);chat.scrollTop=chat.scrollHeight}
function rmTyping(){const e=document.getElementById('typing');if(e)e.remove()}
function showQR(replies){rmQR();const c=document.createElement('div');c.className='qr';c.id='qr';for(const t of replies){const b=document.createElement('button');b.textContent=t;b.onclick=()=>qt(t);c.appendChild(b)}chat.appendChild(c);chat.scrollTop=chat.scrollHeight}
function rmQR(){const e=document.getElementById('qr');if(e)e.remove()}
function ctxQR(lt){if(lt==='satisfaction')return QR.sat;if(lt==='closing')return[];if(lt==='reply')return QR.reply;return QR.def}
function addStars(parent,idx){const d=document.createElement('div');d.className='stars';const l=document.createElement('span');l.className='lbl';l.textContent=currentLang==='cn'?'有帮助？':'Helpful?';d.appendChild(l);for(let i=1;i<=5;i++){const b=document.createElement('button');b.textContent='⭐';b.title=i+'星';b.onclick=()=>rate(idx,i,d);b.onmouseenter=()=>d.querySelectorAll('button').forEach((x,j)=>{if(x.classList.contains('thx'))return;x.style.filter=j<i?'grayscale(0) opacity(1)':'grayscale(1) opacity(0.25)'});b.onmouseleave=()=>{if(!d.querySelector('.on'))d.querySelectorAll('button').forEach(x=>{if(x.classList.contains('thx'))return;x.style.filter='grayscale(1) opacity(0.25)'})};d.appendChild(b)}parent.appendChild(d)}
function addSpeakBtn(parent,text){const b=document.createElement('button');b.textContent='🔊';b.title=currentLang==='cn'?'朗读':'Read aloud';b.style.cssText='background:none;border:none;cursor:pointer;font-size:12px;padding:2px 4px;opacity:0.5;transition:all 0.2s';b.onmouseenter=()=>b.style.opacity=1;b.onmouseleave=()=>b.style.opacity=0.5;b.onclick=()=>speakText(text);parent.appendChild(b)}
function rate(idx,stars,d){d.querySelectorAll('button').forEach((b,j)=>{if(j<stars&&!b.classList.contains('thx'))b.classList.add('on');b.onclick=null;b.style.cursor='default'});const l=d.querySelector('.lbl');if(l)l.remove();const th=document.createElement('div');th.className='thx';th.textContent='感谢评价！'+stars+'⭐';d.replaceWith(th);if(sess)fetch('/api/rating',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sess,message_index:idx,stars})}).catch(()=>{})}
async function sendMsg(text){if(busy)return;const msg=text||inp.value.trim();if(!msg)return;inp.value='';updateCharCount();addMsg('user',msg);busy=true;sbtn.disabled=true;addTyping();try{const s=sess||crypto.randomUUID();if(!sess){sess=s;if(showI)document.getElementById('iSess').textContent=s.slice(0,8)+'...'}const resp=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,session_id:s,stream:true})});rmTyping();if(resp.headers.get('content-type')?.includes('text/event-stream')){await handleStream(resp)}else{const data=await resp.json();if(data.error){addMsg('bot',(currentLang==='cn'?'错误: ':'Error: ')+data.error,'',false)}else{let lt='';for(const r of data.replies){const m={satisfaction:'satisfaction',closing:'closing'};lt=m[r.type]||'reply';addMsg('bot',r.content,lt,true)}const s2=ctxQR(lt);if(s2.length)setTimeout(()=>showQR(s2),800);updInfo(data)}}}catch(e){rmTyping();addMsg('bot',(currentLang==='cn'?'连接错误: ':'Connection error: ')+e.message,'')}busy=false;sbtn.disabled=false;inp.focus()}
async function handleStream(resp){const reader=resp.body.getReader(),dec=new TextDecoder();let buf='',full='',lt='reply',meta=null;const div=addMsg('bot','', 'reply',false);const bub=div.querySelector('.bub');bub.classList.add('cur');const deadline=Date.now()+180000;let finished=false;try{while(Date.now()<deadline&&!finished){const{done,value}=await reader.read();if(done){finished;break}buf+=dec.decode(value,{stream:true});const lines=buf.split('\n');buf=lines.pop()||'';for(const ln of lines){if(!ln.startsWith('data: '))continue;try{const d=JSON.parse(ln.slice(6));if(d.done){meta=d;bub.classList.remove('cur');finished=true;break}else if(d.progress==='analyzing')bub.textContent='🤔 分析中...';else if(d.token!==undefined){full+=d.token;bub.dataset.raw=full;bub.innerHTML=md(full);chat.scrollTop=chat.scrollHeight}}catch(e){}}}if(!meta){bub.classList.remove('cur');lt='reply';updInfo({})}reader.cancel().catch(()=>{})}catch(e){bub.classList.remove('cur')}if(meta){lt=meta.reply_type||'reply';bub.className='bub '+lt;updInfo(meta);const s=ctxQR(lt);if(s.length)setTimeout(()=>showQR(s),800)}}
function updInfo(d){if(!showI)return;if(d.intent)document.getElementById('iIntent').textContent=d.intent;if(d.emotion){const em={neutral:'😐',angry:'😠',sad:'😢',anxious:'😰',happy:'😊'};document.getElementById('iEmo').textContent=(em[d.emotion]||'😐')+' '+d.emotion+(d.emotion_intensity?'('+d.emotion_intensity+'/5)':'');updEmBar(d.emotion,d.emotion_intensity)}document.getElementById('iMsg').textContent=cnt}
function updEmBar(em,intensity){const bar=document.getElementById('emBar');if(!bar||!intensity){bar.innerHTML='';return}let h='';for(let i=1;i<=5;i++)h+='<i class="'+(i<=intensity?(intensity>=4?'on hi':'on'):'')+'"></i>';bar.innerHTML=h}
function newSess(){sess=crypto.randomUUID();if(showI)document.getElementById('iSess').textContent=sess.slice(0,8)+'...';chat.innerHTML='';cnt=0;bidx=0;addSys(t('newSess'));setTimeout(()=>{addMsg('bot',t('welcome'),'reply',true);setTimeout(()=>showQR(QR.def),1000)},300)}
function clearChat(){chat.innerHTML='';cnt=0;bidx=0;const es=document.createElement('div');es.className='empty-state';es.id='emptyState';es.innerHTML='<div class="emoji">💬</div><div class="title">开始对话</div><div class="desc">发送消息开始与智能客服助手交流</div>';chat.appendChild(es)}
function resetAll(){sess=null;cnt=0;bidx=0;chat.innerHTML='';const es=document.createElement('div');es.className='empty-state';es.id='emptyState';es.innerHTML='<div class="emoji">💬</div><div class="title">开始对话</div><div class="desc">发送消息开始与智能客服助手交流</div>';chat.appendChild(es);addSys(currentLang==='cn'?'已重置':'Reset')}
function qt(t){inp.value=t;sendMsg(t)}
async function exportSession(){if(!sess){addSys(t('startFirst'));return}document.getElementById('mContent').textContent=t('loading');document.getElementById('modal').classList.add('show');try{const r=await fetch('/api/export/'+sess);const d=await r.json();if(d.error)document.getElementById('mContent').textContent=(currentLang==='cn'?'错误: ':'Error: ')+d.error;else{window._exp=d;document.getElementById('mContent').textContent=JSON.stringify(d,null,2)}}catch(e){document.getElementById('mContent').textContent=t('netErr')+e.message}}
function closeModal(){document.getElementById('modal').classList.remove('show')}
function cpExport(){navigator.clipboard.writeText(document.getElementById('mContent').textContent).then(()=>addSys(currentLang==='cn'?'已复制':'Copied')).catch(()=>addSys(currentLang==='cn'?'复制失败':'Copy failed'))}
function dlExport(){if(!window._exp)return;const b=new Blob([JSON.stringify(window._exp,null,2)],{type:'application/json'});const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download='session-'+(window._exp.session_id||'export')+'.json';a.click();URL.revokeObjectURL(u)}
function copyAllConversation(){
  const msgs=chat.querySelectorAll('.msg');
  if(!msgs.length){addSys(currentLang==='cn'?'暂无对话可复制':'No conversation to copy');return}
  let lh=currentLang==='cn';
  let text=(lh?'=== 智能客服会话记录 ===':'=== Customer Service Record ===')+'\n';
  text+=(lh?'会话ID: ':'Session ID: ')+(sess?''+sess:(lh?'未设置':'Not set'))+'\n';
  text+=(lh?'时间: ':'Time: ')+new Date().toLocaleString(lh?'zh-CN':'en-US')+'\n';
  text+=(lh?'消息数: ':'Messages: ')+cnt+'\n\n---\n';
  msgs.forEach(function(m){
    const role=m.classList.contains('user')?(lh?'你':'You'):(lh?'客服':'Agent');
    const bub=m.querySelector('.bub');
    if(bub){
      const raw=bub.dataset.raw||bub.textContent;
      const timeStr=new Date().toLocaleTimeString(lh?'zh-CN':'en-US',{hour:'2-digit',minute:'2-digit'});
      text+='['+timeStr+'] '+role+': '+raw+'\n\n';
    }
  });
  text+='---\n'+(lh?'结束':'End');
  navigator.clipboard.writeText(text).then(function(){
    addSys((lh?'✅ 全部对话已复制到剪贴板 ':'✅ All conversation copied to clipboard ')+('('+cnt+(lh?'条消息)':' messages)')));
  }).catch(function(){
    addSys(lh?'❌ 复制失败，请手动选择复制':'❌ Copy failed, please select manually');
  });
}
async function reloadKB(){try{const r=await fetch('/api/rag/reload');const d=await r.json();if(d.error)addSys((currentLang==='cn'?'重载失败: ':'Reload failed: ')+d.error);else addSys('✅ '+(currentLang==='cn'?'知识库已重载: ':'Knowledge base reloaded: ')+d.documents+(currentLang==='cn'?' 文档, ':' documents, ')+d.sections+(currentLang==='cn'?' 章节':' sections'))}catch(e){addSys((currentLang==='cn'?'网络错误: ':'Network error: ')+e.message)}}
let _searchTimer=null;
async function searchSessions(q){if(_searchTimer)clearTimeout(_searchTimer);_searchTimer=setTimeout(async()=>{q=q.trim();if(!q)return;try{const r=await fetch('/api/sessions?search='+encodeURIComponent(q));const d=await r.json();if(d.error){addSys('搜索失败: '+d.error);return}if(!d.sessions.length){addSys('未找到匹配 "'+q+'" 的会话');return}let info='找到 '+d.sessions.length+' 个匹配会话:\n';d.sessions.slice(0,5).forEach(s=>{info+='• ['+s.session_id.slice(0,8)+'] '+s.message_count+'条 · '+s.preview+'\n'});addSys(info)}catch(e){addSys('网络错误: '+e.message)}},300)}
async function runFull(){clearChat();newSess();await new Promise(r=>setTimeout(r,500));const steps=[{msg:'产品怎么用？',label:'步骤1：咨询'},{msg:'谢谢，没问题了',label:'步骤2：结束'},{msg:'满意',label:'步骤3：反馈'}];for(const s of steps){addSys(s.label);await new Promise(r=>setTimeout(r,500));inp.value=s.msg;await sendMsg(s.msg);while(busy)await new Promise(r=>setTimeout(r,200));await new Promise(r=>setTimeout(r,1500))}addSys('演示完成！')}
function scrollToBottom(){chat.scrollTo({top:chat.scrollHeight,behavior:'smooth'})}
function checkScrollPosition(){const sb=document.getElementById('scrollBottom');if(!sb)return;const distFromBottom=chat.scrollHeight-chat.scrollTop-chat.clientHeight;if(distFromBottom>200)sb.classList.add('show');else sb.classList.remove('show')}
let recognition=null;let isListening=false;
function initSpeech(){
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){console.warn('Speech Recognition not supported');return}
  recognition=new SR();recognition.continuous=false;recognition.interimResults=true;
  recognition.lang='zh-CN';
  recognition.onstart=()=>{isListening=true;document.getElementById('vbtn').style.background='#ef4444';document.getElementById('vbtn').title='点击停止录音'};
  recognition.onresult=(e)=>{
    let transcript='';let interim='';
    for(let i=e.resultIndex;i<e.results.length;i++){
      if(e.results[i].isFinal)transcript+=e.results[i][0].transcript;
      else interim+=e.results[i][0].transcript;
    }
    if(transcript)inp.value=transcript;else if(interim)inp.value=interim;
    updateCharCount();
  };
  recognition.onerror=(e)=>{console.error('Speech error:',e.error);stopVoice()};
  recognition.onend=()=>{isListening=false;document.getElementById('vbtn').style.background='#6b7280';document.getElementById('vbtn').title='点击语音输入'};
}
function toggleVoice(){
  if(!recognition)initSpeech();
  if(isListening){stopVoice()}else{startVoice()}
}
function startVoice(){
  if(!recognition)return;
  try{recognition.lang=currentLang==='cn'?'zh-CN':'en-US';recognition.start();document.getElementById('vbtn').title='录音中...'}catch(e){console.error(e)}
}
function stopVoice(){
  if(!recognition||!isListening)return;
  try{recognition.stop()}catch(e){console.error(e)}
}
function speakText(text){
  if(!('speechSynthesis' in window)){console.warn('Speech Synthesis not supported');return}
  window.speechSynthesis.cancel();
  const utter=new SpeechUtterance(text);
  utter.lang=currentLang==='cn'?'zh-CN':'en-US';
  utter.rate=1;utter.pitch=1;
  const voices=speechSynthesis.getVoices();
  const lang=utter.lang.substring(0,2);
  const voice=voices.find(v=>v.lang.startsWith(lang))||voices[0];
  if(voice)utter.voice=voice;
  window.speechSynthesis.speak(utter);
}
if('speechSynthesis' in window){speechSynthesis.onvoiceschanged=()=>{}}
window.addEventListener('DOMContentLoaded',()=>{if(!sess)newSess();chat.addEventListener('scroll',checkScrollPosition,{passive:true});initSpeech()});
