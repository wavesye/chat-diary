const $ = id => document.getElementById(id);
const state = { busy: false, messages: [], day: "", calendarMonth: "", reviewDays: new Map() };

function escapeHtml(value="") { const node=document.createElement("div"); node.textContent=value; return node.innerHTML; }
function markdown(value="") {
  let text=escapeHtml(value).replace(/^---[\s\S]*?---\s*/, "");
  return text.split("\n").map(line => {
    if (line.startsWith("# ")) return `<h1>${line.slice(2)}</h1>`;
    if (line.startsWith("## ")) return `<h2>${line.slice(3)}</h2>`;
    if (line.startsWith("> ")) return `<blockquote>${line.slice(2)}</blockquote>`;
    if (line.startsWith("- [ ] ")) return `<li>☐ ${line.slice(6)}</li>`;
    if (line === "---") return "<hr>";
    return line.trim() ? `<p>${line}</p>` : "";
  }).join("");
}
function showToast(text) { const el=$("toast"); el.textContent=text; el.classList.add("show"); clearTimeout(showToast.timer); showToast.timer=setTimeout(()=>el.classList.remove("show"),3200); }
function scrollBottom() { requestAnimationFrame(()=>$("chat").scrollTop=$("chat").scrollHeight); }
function messageNode(message) {
  const article=document.createElement("article"); article.className=`message ${message.role}`;
  const bubble=`<div class="bubble">${escapeHtml(message.content)}</div>`;
  article.innerHTML=message.role==="assistant"?`<div class="avatar">拾</div>${bubble}`:bubble;
  if(message.id){const button=document.createElement("button");button.className="delete-message";button.setAttribute("aria-label","删除这条对话");button.title="删除这条对话";button.textContent="×";button.onclick=()=>removeMessage(message.id);article.append(button)}
  return article;
}
function render() {
  $("welcome").hidden=state.messages.length>0; $("messages").innerHTML="";
  state.messages.forEach(message=>$("messages").append(messageNode(message)));
  const userCount=state.messages.filter(m=>m.role==="user").length;
  $("messageCount").textContent=userCount?`已经聊了 ${userCount} 句`:"还没有开始"; scrollBottom();
}
async function request(path, options={}) {
  const response=await fetch(path,options); if(!response.ok){let message=`请求失败 (${response.status})`;try{message=(await response.json()).detail||message}catch{}throw new Error(message)} return response.json();
}
async function bootstrap() {
  const data=await request("/v1/session"); state.messages=data.messages; state.day=data.day; state.calendarMonth=data.day.slice(0,7); $("greeting").textContent=data.greeting;
  const date=new Date(`${data.day}T12:00:00`); $("monthDay").textContent=String(date.getDate()).padStart(2,"0");
  $("weekday").textContent=new Intl.DateTimeFormat("zh-CN",{weekday:"long"}).format(date);
  $("fullDate").textContent=new Intl.DateTimeFormat("zh-CN",{year:"numeric",month:"long"}).format(date); render();
}
function setView(view){
  const review=view==="review"; $("chat").hidden=review; $("reviewCalendar").hidden=!review;
  document.querySelector(".composer-area").hidden=review;
  $("chatNav").classList.toggle("active",!review); $("reviewNav").classList.toggle("active",review);
  if(review) loadCalendar().catch(error=>showToast(error.message));
  if(innerWidth<=720) $("sidebar").classList.remove("open");
}
function monthLabel(month){const [year,value]=month.split("-").map(Number);return `${year}年${value}月`}
function moveMonth(delta){
  const [year,month]=state.calendarMonth.split("-").map(Number);const next=new Date(year,month-1+delta,1);
  const value=`${next.getFullYear()}-${String(next.getMonth()+1).padStart(2,"0")}`;
  if(value>state.day.slice(0,7))return;state.calendarMonth=value;loadCalendar().catch(error=>showToast(error.message));
}
async function loadCalendar(){
  const data=await request(`/v1/review/month?month=${state.calendarMonth}`);state.reviewDays=new Map(data.days.map(item=>[item.day,item]));
  $("calendarTitle").textContent=monthLabel(state.calendarMonth);$("nextMonth").disabled=state.calendarMonth>=state.day.slice(0,7);renderCalendar();
}
function renderCalendar(){
  const grid=$("calendarGrid");grid.innerHTML="";const [year,month]=state.calendarMonth.split("-").map(Number);
  const first=new Date(year,month-1,1),count=new Date(year,month,0).getDate(),offset=(first.getDay()+6)%7;
  for(let i=0;i<offset;i++){const blank=document.createElement("span");blank.className="calendar-blank";grid.append(blank)}
  for(let value=1;value<=count;value++){
    const day=`${state.calendarMonth}-${String(value).padStart(2,"0")}`,info=state.reviewDays.get(day),future=day>state.day;
    const button=document.createElement("button");button.className="calendar-day"+(info?" has-activity":"")+(day===state.day?" today":"");button.disabled=future;
    button.innerHTML=`<span>${value}</span>${info?`<small>${info.activity_count} 项记录</small>`:"<small></small>"}`;
    if(!future)button.onclick=()=>loadDayReview(day,button);grid.append(button);
  }
}
async function loadDayReview(day,button){
  document.querySelectorAll(".calendar-day.selected").forEach(item=>item.classList.remove("selected"));button.classList.add("selected");
  const data=await request(`/v1/review/day/${day}`),activities=[...data.completed_todos,...data.other_activities];
  const sections=[];if(data.title)sections.push(`<h2>${escapeHtml(data.title)}</h2>`);
  if(data.completed_todos.length)sections.push(`<h3>完成的 Todo</h3><ul>${data.completed_todos.map(item=>`<li>${escapeHtml(item.title)}</li>`).join("")}</ul>`);
  if(data.other_activities.length)sections.push(`<h3>其他活动</h3><ul>${data.other_activities.map(item=>`<li>${escapeHtml(item.title)}<small>${escapeHtml(item.description||"")}</small></li>`).join("")}</ul>`);
  if(data.tags.length)sections.push(`<div class="review-tags">${data.tags.map(tag=>`<span>#${escapeHtml(tag)}</span>`).join("")}</div>`);
  $("dayReview").innerHTML=`<time>${day}</time>${sections.join("")||'<p class="review-empty">这一天还没有提取到活动记录。</p>'}`;
}
async function syncMessages(){const data=await request("/v1/session");state.messages=data.messages;render()}
async function removeMessage(id){if(state.busy||!confirm("删除这条对话吗？删除后，今天已有的长期记忆会等待下次写入日记时重建。"))return;try{await request(`/v1/messages/${id}`,{method:"DELETE"});await syncMessages();showToast("已删除；如已生成日记，请重新写入 Obsidian")}catch(error){showToast(error.message)}}
async function send() {
  if(state.busy)return; const input=$("messageInput"),message=input.value.trim(); if(!message)return;
  const command=message.toLowerCase();
  if(command==="/preview"){input.value="";resize();await preview();return}
  if(command==="/done"){input.value="";resize();await finish();return}
  if(command==="/help"){input.value="";resize();showToast("/preview 预览今日手记 · /done 写入 Obsidian");return}
  state.messages.push({role:"user",content:message}); input.value=""; resize(); render(); state.busy=true; $("sendButton").disabled=true;
  const thinking=document.createElement("article"); thinking.className="message assistant"; thinking.id="thinking"; thinking.innerHTML='<div class="avatar">拾</div><div class="bubble thinking"><i></i><i></i><i></i></div>'; $("messages").append(thinking); scrollBottom();
  try { await request("/v1/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message})}); await syncMessages(); }
  catch(error){state.messages.pop();showToast(error.message)} finally {state.busy=false;$("sendButton").disabled=false;render();input.focus()}
}
function openPreview(){ $("preview").classList.add("open"); $("preview").setAttribute("aria-hidden","false"); $("overlay").classList.add("show"); }
function closePreview(){ $("preview").classList.remove("open"); $("preview").setAttribute("aria-hidden","true"); $("overlay").classList.remove("show"); }
async function preview(){openPreview();$("paper").innerHTML='<p class="paper-empty">正在把今天的谈话整理成一篇日记……</p>';try{const data=await request("/v1/preview",{method:"POST"});$("paper").innerHTML=markdown(data.markdown)}catch(error){$("paper").innerHTML=`<p class="paper-empty">${escapeHtml(error.message)}</p>`}}
async function finish(){if(!state.messages.some(m=>m.role==="user")){showToast("先聊几句，再写下今天吧");return}const button=$("finishButton");button.disabled=true;try{const data=await request("/v1/finalize",{method:"POST"});showToast(`已写入 Obsidian · ${data.day}`);closePreview()}catch(error){showToast(error.message)}finally{button.disabled=false}}
function resize(){const el=$("messageInput");el.style.height="auto";el.style.height=`${Math.min(el.scrollHeight,150)}px`}
function theme(value){document.documentElement.dataset.theme=value;localStorage.setItem("diary-theme",value);$("themeButton").textContent=value==="dark"?"☾":"☼"}

$("sendButton").onclick=send; $("messageInput").oninput=resize; $("messageInput").onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}};
document.querySelectorAll("[data-text]").forEach(button=>button.onclick=()=>{$("messageInput").value=button.dataset.text;resize();$("messageInput").focus()});
$("previewButton").onclick=preview; $("finishButton").onclick=finish; $("saveFromPreview").onclick=finish; $("closePreview").onclick=closePreview; $("overlay").onclick=closePreview;
$("themeButton").onclick=()=>theme(document.documentElement.dataset.theme==="dark"?"light":"dark"); $("mobileMenu").onclick=()=>$("sidebar").classList.toggle("open");
$("chatNav").onclick=()=>setView("chat");$("reviewNav").onclick=()=>setView("review");$("previousMonth").onclick=()=>moveMonth(-1);$("nextMonth").onclick=()=>moveMonth(1);
theme(localStorage.getItem("diary-theme")||"light"); bootstrap().catch(error=>showToast(error.message));
