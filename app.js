// HIVV.org shared JavaScript - part 1: setup, status, problems
let myAgentId = localStorage.getItem('hivv_agent_id');
if (!myAgentId) { myAgentId = 'web-' + Math.random().toString(36).slice(2, 10); localStorage.setItem('hivv_agent_id', myAgentId); }
let conversationHistory = [];
let hivvSessionToken = localStorage.getItem('hivv_session_token') || null;
function escapeHtml(t) { if (t === null || t === undefined) return ''; const d = document.createElement('div'); d.textContent = String(t); return d.innerHTML; }async function updateSystemStatus(){try{const res=await fetch('/api/system/status');const data=await res.json();const cpu=data.cpu;const freePct=Math.round(cpu.freeRatio*100);const cpuColor=cpu.canAcceptWork?'#00ff88':(freePct<15?'#ff6b6b':'#ffd93d');const cpuEl=document.getElementById('cpu-status');if(cpuEl)cpuEl.innerHTML='<span style="color:'+cpuColor+';">'+freePct+'%</span> (load '+cpu.load1.toFixed(1)+'/'+cpu.threshold+')';const gpuEl=document.getElementById('gpu-status');if(gpuEl&&data.gpu.available){const gpuPct=Math.round(data.gpu.utilization);const gpuColor=gpuPct<80?'#00ff88':'#ffd93d';gpuEl.innerHTML='<span style="color:'+gpuColor+';">'+gpuPct+'%</span>'; ('+data.gpu.memoryUsed+'/'+data.gpu.memoryTotal+' MiB)';}else if(gpuEl)gpuEl.textContent='N/A';const jobsEl=document.getElementById('jobs-status');if(jobsEl)jobsEl.innerHTML=data.jobs.active+' active, '+data.jobs.totalProcessed+' total';}catch(e){}}
async function updatePoolCount(){const poolEl=document.getElementById('pool-status');if(!poolEl)return;try{const res=await fetch('/api/quota');const data=await res.json();poolEl.textContent=(data.providers&&data.providers.external_pool?data.providers.external_pool.length:0);}catch(e){}}
async function updateNodeCount(){const el=document.getElementById('nodes-status');if(!el)return;try{const res=await fetch('/api/agents/online');const data=await res.json();const n=data.online_count||0;el.textContent=n;el.style.color=n>0?'#74b9ff':'#555';}catch(e){}}
async function submitProblem(){const title=document.getElementById('problem-title').value.trim();const desc=document.getElementById('problem-desc').value.trim();const priority=document.getElementById('problem-priority').value;const btn=document.getElementById('submit-problem-btn');const resultDiv=document.getElementById('submit-result');if(!title||!desc){resultDiv.style.display='block';resultDiv.innerHTML='<span style="color:#ff6b6b;">Title and description are required.</span>';return;}btn.disabled=true;btn.textContent='Submitting...';try{const res=await fetch('/api/problems/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title,description:desc,priority:parseInt(priority)})});const data=await res.json();if(data.success){const color=data.queued?'#ffd93d':'#00ff88';resultDiv.style.display='block';resultDiv.innerHTML='<div style="background:#0d2818;border:1px solid '+color+'44;border-radius:6px;padding:12px;"><span style="color:'+color+';font-weight:bold;">Problem #'+data.problem_id+' submitted!</span> <span style="color:#888;">&mdash; '+escapeHtml(title)+'</span><div style="color:#ccc;font-size:0.85em;margin-top:5px;">'+escapeHtml(data.message)+'</div></div>';document.getElementById('problem-title').value='';document.getElementById('problem-desc').value='';if(typeof loadProblems==='function')loadProblems();}else{resultDiv.style.display='block';resultDiv.innerHTML='<span style="color:#ff6b6b;">Error: '+escapeHtml(data.error||'Unknown')+'</span>';}}catch(err){resultDiv.style.display='block';resultDiv.innerHTML='<span style="color:#ff6b6b;">Connection error: '+escapeHtml(err.message)+'</span>';}finally{btn.disabled=false;btn.textContent='Submit Problem';}}
async function loadProblems(){const listDiv=document.getElementById('problems-list');if(!listDiv)return;try{const res=await fetch('/api/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sql:"SELECT id, title, status, priority, created_at FROM problems ORDER BY priority DESC, id DESC LIMIT 20"})});const data=await res.json();if(data.rows&&data.rows.length>0){listDiv.innerHTML=data.rows.map(function(p){const date=new Date(p.created_at).toLocaleDateString();return '<div onclick="openProblem('+p.id+')" class="clickable" style="background:#1a1a24;border-radius:6px;padding:12px;margin-bottom:8px;border-left:3px solid #74b9ff;cursor:pointer;"><div style="display:flex;justify-content:space-between;align-items:center;"><span style="color:#e0e0e0;font-weight:bold;">#'+p.id+': '+escapeHtml(p.title)+'</span><span class="badge badge-'+p.status+'">'+p.status+'</span></div><div style="color:#666;font-size:0.85em;margin-top:4px;">Priority: '+p.priority+' | Created: '+date+' | Click for details</div></div>';}).join('');}else{listDiv.innerHTML='<span style="color:#666;">No problems yet. Submit one above!</span>';}}catch(err){listDiv.innerHTML='<span style="color:#ff6b6b;">Failed to load: '+escapeHtml(err.message)+'</span>';}}
let currentProblem=null;
async function openProblem(id) {
  currentProblem = id;
  const modal = document.getElementById('problem-modal');
  if (!modal) return;
  modal.style.display = 'block';
  document.getElementById('modal-title').textContent = 'Loading...';
  document.getElementById('modal-description').textContent = '';
  document.getElementById('modal-tasks').innerHTML = '';
  document.getElementById('modal-contributions').innerHTML = '';
  switchTab('tasks');
  try {
    const problemRes = await fetch('/api/query', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sql: 'SELECT id, title, description, status, priority, created_at FROM problems WHERE id = ' + id})
    });
    const problemData = await problemRes.json();
    if (problemData.rows && problemData.rows[0]) {
      const p = problemData.rows[0];
      document.getElementById('modal-title').textContent = '#' + p.id + ': ' + p.title;
      document.getElementById('modal-status').innerHTML = '<span class="badge badge-' + p.status + '">' + p.status + '</span> Priority: ' + p.priority;
      document.getElementById('modal-description').textContent = p.description;
    }
    const tasksRes = await fetch('/api/query', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sql: 'SELECT id, status, description, assigned_agent FROM tasks WHERE problem_id = ' + id + ' ORDER BY id'})
    });
    const tasksData = await tasksRes.json();
    let tasksHtml = '';
    if (tasksData.rows && tasksData.rows.length > 0) {
      for (const t of tasksData.rows) {
        const desc = (t.description || '').slice(0, 100);
        const agentHtml = t.assigned_agent ? ' <span style="color:#888;">&mdash; ' + escapeHtml(t.assigned_agent) + '</span>' : '';
        tasksHtml += '<div style="background:#1a1a24;border-radius:6px;padding:10px;margin-bottom:6px;border-left:3px solid #74b9ff;"><span class="badge badge-' + t.status + '">' + t.status + '</span> ' + escapeHtml(desc) + agentHtml + '</div>';
      }
    } else {
      tasksHtml = '<span style="color:#666;">No tasks yet</span>';
    }
    document.getElementById('modal-tasks').innerHTML = tasksHtml;
    const contribRes = await fetch('/api/query', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sql: 'SELECT id, agent_id, content, vote_score FROM contributions WHERE problem_id = ' + id + ' ORDER BY vote_score DESC, id DESC'})
    });
    const contribData = await contribRes.json();
    let contribHtml = '';
    if (contribData.rows && contribData.rows.length > 0) {
      for (const c of contribData.rows) {
        let ans = '';
        if (c.content && c.content.answer !== undefined && c.content.answer !== null) {
          ans = (typeof c.content.answer === 'string') ? c.content.answer : JSON.stringify(c.content.answer);
        } else if (c.content) {
          ans = JSON.stringify(c.content);
        }
        const truncated = ans.length > 800;
        contribHtml += '<div style="background:#1a1a24;border-radius:6px;padding:12px;margin-bottom:8px;border-left:3px solid #00ff88;"><div style="display:flex;justify-content:space-between;margin-bottom:6px;"><span style="color:#ffd93d;">' + escapeHtml(c.agent_id) + '</span><span style="color:#00ff88;">&#9650; ' + c.vote_score + '</span></div><div style="color:#ccc;font-size:0.9em;white-space:pre-wrap;">' + escapeHtml(truncated ? ans.slice(0, 800) : ans) + (truncated ? '...' : '') + '</div></div>';
      }
    } else {
      contribHtml = '<span style="color:#666;">No contributions yet &mdash; AI is working on it...</span>';
    }
    document.getElementById('modal-contributions').innerHTML = contribHtml;
  } catch (err) {
    document.getElementById('modal-description').innerHTML = '<span style="color:#ff6b6b;">Error: ' + escapeHtml(err.message) + '</span>';
  }
}
function switchTab(tabName){const tasksContent=document.getElementById('tab-content-tasks');const activityContent=document.getElementById('tab-content-activity');const tasksTab=document.getElementById('tab-tasks');const activityTab=document.getElementById('tab-activity');if(!tasksContent||!activityContent)return;if(tabName==='tasks'){tasksContent.style.display='block';activityContent.style.display='none';if(tasksTab)tasksTab.style.color='#74b9ff';if(activityTab)activityTab.style.color='#888';}else{tasksContent.style.display='none';activityContent.style.display='block';if(tasksTab)tasksTab.style.color='#888';if(activityTab)activityTab.style.color='#74b9ff';loadProblemActivity();}}
async function loadProblemActivity(){if(!currentProblem)return;const el=document.getElementById('modal-activity');if(!el)return;el.innerHTML='<span style="color:#666;">Loading activity...</span>';try{const res=await fetch('/api/activity/problem/'+currentProblem);const data=await res.json();if(!data.events||data.events.length===0){el.innerHTML='<span style="color:#666;">No activity yet for this problem.</span>';return;}const eventColors={'chat_question':{icon:'?',color:'#00ff88',label:'Question'},'chat_answer':{icon:'A',color:'#74b9ff',label:'AI Answer'},'task_claimed':{icon:String.fromCharCode(9881),color:'#ffd93d',label:'Task Claimed'},'task_done':{icon:String.fromCharCode(10003),color:'#00ff88',label:'Task Done'},'task_failed':{icon:String.fromCharCode(10007),color:'#ff6b6b',label:'Task Failed'},'contribution_added':{icon:'+',color:'#00ff88',label:'Contribution'},'vote_cast':{icon:String.fromCharCode(9650),color:'#74b9ff',label:'Vote'},'problem_submitted':{icon:'*',color:'#c084fc',label:'Submitted'},'note':{icon:'.',color:'#888',label:'Note'}};el.innerHTML=data.events.map(function(ev){const meta=eventColors[ev.event_type]||{icon:'.',color:'#888',label:ev.event_type};const dateStr=new Date(ev.created_at).toLocaleString();const actor=ev.actor||'anonymous';let body='';if(ev.event_type==='chat_question'){body='<div style="color:#ccc;margin-top:6px;padding:8px;background:#1a2a1a;border-radius:4px;">'+escapeHtml((ev.payload&&ev.payload.question)||'')+'</div>';}else if(ev.event_type==='chat_answer'){const ans=(ev.payload&&ev.payload.answer)||'';body='<div style="color:#ccc;margin-top:6px;padding:8px;background:#1a1a2a;border-radius:4px;white-space:pre-wrap;">'+escapeHtml(ans.slice(0,600))+(ans.length>600?'...':'')+'</div>';if(ev.payload&&ev.payload.provider)body+='<div style="color:#666;font-size:0.8em;margin-top:4px;">via '+escapeHtml(ev.payload.provider)+'</div>';}else if(ev.event_type==='contribution_added'){const preview=(ev.payload&&ev.payload.answer_preview)||'';body='<div style="color:#888;font-size:0.85em;margin-top:4px;">'+escapeHtml(preview)+(preview.length>=200?'...':'')+'</div>';}else{const payloadStr=ev.payload?JSON.stringify(ev.payload):'';body='<div style="color:#888;font-size:0.85em;margin-top:4px;">'+escapeHtml(payloadStr.slice(0,300))+'</div>';}return '<div style="background:#1a1a24;border-radius:6px;padding:12px;margin-bottom:8px;border-left:3px solid '+meta.color+'"><div style="display:flex;justify-content:space-between;align-items:center;"><span><span style="color:'+meta.color+';font-weight:bold;">'+meta.icon+'</span> <span style="color:'+meta.color+'">'+meta.label+'</span> by <span style="color:#ffd93d;">'+escapeHtml(actor)+'</span></span><span style="color:#666;font-size:0.8em;">'+dateStr+'</span></div>'+body+'</div>';}).join('');}catch(err){el.innerHTML='<span style="color:#ff6b6b;">Failed to load activity: '+escapeHtml(err.message)+'</span>';}}
function closeProblemModal(){const modal=document.getElementById('problem-modal');if(modal)modal.style.display='none';currentProblem=null;switchTab('tasks');}
function askAIAboutProblem() {
  if (!currentProblem) return;
  const title = document.getElementById('modal-title').textContent || ('Problem #' + currentProblem);
  const description = document.getElementById('modal-description').textContent || '';
  closeProblemModal();
  const prompt = 'Here is a problem from HIVV.org that I want your help on:\n\nTitle: ' + title + '\n\nDescription: ' + description + '\n\nPlease analyze this problem and give me your thoughts or a solution.';
  setTimeout(function() { sendMessageDirect(prompt, currentProblem); }, 50);
  setTimeout(function() {
    const chatEl = document.getElementById('ai-chat');
    if (chatEl) chatEl.scrollIntoView({behavior: 'smooth', block: 'start'});
  }, 100);
}
async function sendChat(){const chatInput=document.getElementById('chat-input');const message=chatInput?chatInput.value.trim():'';if(!message)return;sendMessageDirect(message,null);}
function sendMessageDirect(message, problem_id) {
  const chatMessages = document.getElementById('chat-messages');
  const chatInput = document.getElementById('chat-input');
  const chatSend = document.getElementById('chat-send');
  const chatProvider = document.getElementById('chat-provider');
  if (!message) return;
  if (chatInput) chatInput.value = '';
  if (chatSend) { chatSend.disabled = true; chatSend.textContent = '...'; }
  const userDiv = document.createElement('div');
  userDiv.style.cssText = 'margin-bottom:12px;padding:8px;border-radius:6px;background:#1a2a1a;';
  userDiv.innerHTML = '<span style="color:#00ff88;font-weight:bold;">You:</span> <span style="color:#ccc;">' + escapeHtml(message) + '</span>';
  if (chatMessages) { chatMessages.appendChild(userDiv); chatMessages.scrollTop = chatMessages.scrollHeight; }
  conversationHistory.push({role: 'user', content: message});
  const aiDiv = document.createElement('div');
  aiDiv.style.cssText = 'margin-bottom:12px;padding:8px;border-radius:6px;background:#1a1a2a;';
  aiDiv.innerHTML = '<span style="color:#74b9ff;font-weight:bold;">AI:</span> <span style="color:#666;">Thinking...</span>';
  if (chatMessages) { chatMessages.appendChild(aiDiv); chatMessages.scrollTop = chatMessages.scrollHeight; }
  const providerSelect = document.getElementById('ai-provider-select');
  const selectedProvider = providerSelect ? providerSelect.value : 'auto';
  const fetchHeaders = {'Content-Type': 'application/json'};
  if (hivvSessionToken) fetchHeaders['X-Session-Token'] = hivvSessionToken;
  const body = {messages: conversationHistory, agent_id: myAgentId, problem_id: problem_id || null};
  if (selectedProvider && selectedProvider !== 'auto' && selectedProvider !== 'local') {
    body.provider = selectedProvider;
  }
  fetch('/api/chat', {
    method: 'POST',
    headers: fetchHeaders,
    body: JSON.stringify(body)
  }).then(function(res) {
    const provider = res.headers.get('X-AI-Provider') || 'unknown';
    const quota = res.headers.get('X-AI-Quota-Remaining');
    if (chatProvider) chatProvider.textContent = 'Provider: ' + provider + (quota ? ' | Quota remaining: ' + quota : ' (local, no quota used)');
    if (!res.ok) {
      return res.text().then(function(text) { throw new Error(text.slice(0, 200)); }).catch(function() { throw new Error('HTTP ' + res.status); });
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let fullResponse = '';
    let buffer = '';
    function read() {
      reader.read().then(function(r) {
        if (r.done) {
          if (fullResponse) conversationHistory.push({role: 'assistant', content: fullResponse});
          if (chatSend) { chatSend.disabled = false; chatSend.textContent = 'Send'; }
          return;
        }
        buffer += decoder.decode(r.value, {stream: true});
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (let i = 0; i < lines.length; i++) {
          const line = lines[i];
          if (line.startsWith('data: ')) {
            const data = line.slice(6).trim();
            if (data === '[DONE]') continue;
            try {
              const parsed = JSON.parse(data);
              const content = (parsed.choices && parsed.choices[0] && (parsed.choices[0].delta.content || parsed.choices[0].message.content)) || (parsed.data && parsed.data.text) || '';
              if (content) {
                fullResponse += content;
                aiDiv.querySelector('span:last-child').textContent = fullResponse;
                if (chatMessages) chatMessages.scrollTop = chatMessages.scrollHeight;
              }
            } catch (e) {}
          }
        }
        read();
      }).catch(function(err) {
        aiDiv.querySelector('span:last-child').innerHTML = '<span style="color:#ff6b6b;">Connection error: ' + escapeHtml(err.message) + '</span>';
        if (chatSend) { chatSend.disabled = false; chatSend.textContent = 'Send'; }
      });
    }
    read();
  }).catch(function(err) {
    aiDiv.querySelector('span:last-child').innerHTML = '<span style="color:#ff6b6b;">Error: ' + escapeHtml(err.message) + '</span>';
    if (chatSend) { chatSend.disabled = false; chatSend.textContent = 'Send'; }
  });
}
function onPageLoad(){updateSystemStatus();updatePoolCount();updateNodeCount();if(typeof loadProblems==='function')loadProblems();checkChatAuth();}
async function checkChatAuth(){
  const statusEl=document.getElementById('chat-auth-status');
  if(!statusEl)return;
  if(!hivvSessionToken){statusEl.textContent='(not logged in — using public pool)';return;}
  try{
    const res=await fetch('/api/auth/me',{headers:{'X-Session-Token':hivvSessionToken}});
    const data=await res.json();
    if(data.logged_in){
      statusEl.innerHTML='(logged in as <span style="color:#00ff88;">'+escapeHtml(data.user.username)+'</span> &mdash; your keys used first)';
      const navAcc=document.getElementById('nav-account');
      if(navAcc)navAcc.style.color='#00ff88';
    }else{
      hivvSessionToken=null;
      localStorage.removeItem('hivv_session_token');
      statusEl.textContent='(not logged in — using public pool)';
    }
  }catch(e){statusEl.textContent='';}
}

async function loadProviderOptions(){
  const select=document.getElementById('ai-provider-select');
  if(!select)return;
  try{
    const res=await fetch('/api/providers');
    const data=await res.json();
    if(data.providers&&data.providers.length>0){
      const grouped={cloud:[],local:[]};
      for(const p of data.providers){
        if(p.is_cloud===false||p.name==='local'){
          grouped.local.push(p);
        }else{
          grouped.cloud.push(p);
        }
      }
      let html='<option value="auto">Auto (try all available)</option>';
      if(grouped.local.length>0){
        html+='<option value="local">Local AI (auto-detect)</option>';
        for(const p of grouped.local){
          html+='<option value="'+p.name+'">'+escapeHtml(p.display_name)+'</option>';
        }
      }
      for(const p of grouped.cloud){
        html+='<option value="'+p.name+'">'+escapeHtml(p.display_name)+'</option>';
      }
      select.innerHTML=html;
    }
  }catch(e){console.error('Failed to load providers:',e);}
}
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',onPageLoad);}else{onPageLoad();}
setInterval(updateSystemStatus,10000);
setInterval(updateNodeCount,15000);
loadProviderOptions();
