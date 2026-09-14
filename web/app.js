const $ = id => document.getElementById(id);
const state = { busy: false, messages: [], day: "", calendarMonth: "", reviewDays: new Map(), draft: null, draftMode: "read", editorVisited: false };

function escapeHtml(value="") { const node=document.createElement("div"); node.textContent=value; return node.innerHTML; }
function markdown(value="") {
  let text=escapeHtml(value).replace(/^---[\s\S]*?---\s*/, "");
  return text.split("\n").map(line => {
    if (line.startsWith("# ")) return `<h1>${line.slice(2)}</h1>`;
    if (line.startsWith("## ")) return `<h2>${line.slice(3)}</h2>`;
    if (line.startsWith("### ")) return `<h3>${line.slice(4)}</h3>`;
    if (line.startsWith("&gt; ")) return `<blockquote>${line.slice(5)}</blockquote>`;
    if (line.startsWith("- [ ] ")) return `<li>☐ ${line.slice(6)}</li>`;
    if (/^- \[[xX]\] /.test(line)) return `<li>☑ ${line.slice(6)}</li>`;
    if (line.startsWith("- ")) return `<li>• ${line.slice(2)}</li>`;
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
async function syncMessages(){const data=await request("/v1/session");state.messages=data.messages;state.day=data.day;render()}
function setBusy(busy){
  state.busy=busy;
  ["sendButton","previewButton","finishButton","saveFromPreview","refreshDraft"].forEach(id=>$(id).disabled=busy);
  $("draftEditor").readOnly=busy;
  updateDraftControls();
}
async function removeMessage(id){
  if(state.busy||!confirm("删除这条对话吗？删除后，今天已有的长期记忆会等待下次写入日记时重建。"))return;
  setBusy(true);
  try{await request(`/v1/messages/${id}`,{method:"DELETE"});await syncMessages();await syncDraft();showToast("已删除；已有草稿可重新生成或手动修改")}
  catch(error){showToast(error.message)}finally{setBusy(false)}
}
async function send() {
  if(state.busy)return; const input=$("messageInput"),message=input.value; if(!message.trim())return;
  const command=message.trim().toLowerCase();
  if(command==="/preview"){input.value="";resize();await preview();return}
  if(command==="/preview refresh"){input.value="";resize();await preview(true);return}
  if(command==="/done"){input.value="";resize();await finish();return}
  if(command==="/calendar"){input.value="";resize();setView("review");return}
  const pendingMessage={role:"user",content:message};
  state.messages.push(pendingMessage); input.value=""; resize(); render(); setBusy(true);
  const thinking=document.createElement("article"); thinking.className="message assistant"; thinking.id="thinking"; thinking.innerHTML='<div class="avatar">拾</div><div class="bubble thinking"><i></i><i></i><i></i></div>'; $("messages").append(thinking); scrollBottom();
  let sent=false;
  try {
    const data=await request("/v1/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message})});
    sent=true;
    await syncMessages();
    const lastAssistant=state.messages.filter(item=>item.role==="assistant").at(-1);
    if(data.reply&&lastAssistant?.content!==data.reply)state.messages.push({role:"assistant",content:data.reply});
    await syncDraft();
  }
  catch(error){showToast(error.message);if(!sent){state.messages=state.messages.filter(item=>item!==pendingMessage);if(!input.value)input.value=message;resize()}}
  finally {setBusy(false);render();input.focus()}
}
function openPreview(){ $("preview").classList.add("open"); $("preview").setAttribute("aria-hidden","false"); $("overlay").classList.add("show"); }
function closePreview(){ $("preview").classList.remove("open"); $("preview").setAttribute("aria-hidden","true"); $("overlay").classList.remove("show"); }
function draftDirty(){return !!state.draft&&state.draft.markdown!==state.draft.savedMarkdown}
function updateDraftControls(){
  const exists=!!state.draft;
  $("saveDraft").disabled=state.busy||!exists||!draftDirty();
  $("editDraft").disabled=state.busy||!exists;
  $("renderDraft").disabled=state.busy||!exists;
  $("draftStatus").classList.toggle("stale",!!state.draft?.stale);
  if(!exists)return;
  const status=draftDirty()?"有未保存的修改；写入时会先保存。":state.draft.edited?"手动修改已保存。":"草稿已保存。";
  $("draftStatus").textContent=status+(state.draft.stale?" 今天的记录有变化，当前草稿尚未更新；可手动补充或重新生成。":" 写入 Obsidian 时会使用这份草稿。");
}
function showDraft(){
  if(!state.draft)return;
  $("paper").innerHTML=markdown(state.draft.markdown);
  $("draftEditor").value=state.draft.markdown;
  const editing=state.draftMode==="edit";
  $("paper").hidden=editing;$("draftEditor").hidden=!editing;
  $("renderDraft").setAttribute("aria-selected",String(!editing));
  $("editDraft").setAttribute("aria-selected",String(editing));
  updateDraftControls();
}
function acceptDraft(data){if(state.draft?.markdown!==data.markdown)state.editorVisited=false;state.draft={...data,savedMarkdown:data.markdown};showDraft()}
async function syncDraft(){
  if(!state.draft)return;
  try{
    const data=await request("/v1/preview",{method:"POST"});
    if(draftDirty()){
      state.draft.stale=data.stale;
      if(data.markdown!==state.draft.savedMarkdown)showToast("草稿已在其他操作中更新；你当前未保存的编辑仍保留");
      updateDraftControls();
    }else acceptDraft(data);
  }catch(error){showToast(`草稿状态更新失败：${error.message}`)}
}
async function preview(refresh=false){
  if(state.busy)return;
  if(refresh&&!confirm("重新生成会替换现有草稿，包括手动修改的内容。确定重新生成吗？"))return;
  openPreview();
  if(!refresh&&draftDirty()){showDraft();return}
  setBusy(true);
  if(!state.draft)$("paper").innerHTML='<p class="paper-empty">正在整理今天的记录……</p>';
  $("draftStatus").textContent=refresh?"正在重新生成草稿……":"正在读取草稿……";
  try{acceptDraft(await request(`/v1/preview${refresh?"?refresh=true":""}`,{method:"POST"}))}
  catch(error){if(!state.draft)$("paper").innerHTML=`<p class="paper-empty">${escapeHtml(error.message)}</p>`;showToast(error.message);$("draftStatus").textContent="暂时无法读取草稿，请稍后重试。"}
  finally{setBusy(false)}
}
async function persistDraft(){
  if(!draftDirty())return;
  const data=await request("/v1/preview",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({markdown:state.draft.markdown})});
  acceptDraft(data);
}
async function saveDraft(){
  if(state.busy||!draftDirty())return;
  setBusy(true);
  try{await persistDraft();showToast("草稿已保存")}
  catch(error){showToast(error.message)}finally{setBusy(false)}
}
async function finish(){
  if(state.busy)return;
  setBusy(true);
  try{await persistDraft();const data=await request("/v1/finalize",{method:"POST"});showToast(`已写入 Obsidian · ${data.day}${data.stale?" · 保存的是已有草稿，新增记录需重新生成或手动补充":""}`);closePreview()}
  catch(error){showToast(error.message)}finally{setBusy(false)}
}
function resize(){const el=$("messageInput");el.style.height="auto";el.style.height=`${Math.min(el.scrollHeight,150)}px`}
function theme(value){document.documentElement.dataset.theme=value;localStorage.setItem("diary-theme",value);$("themeButton").textContent=value==="dark"?"☾":"☼"}

$("sendButton").onclick=send; $("messageInput").oninput=resize; $("messageInput").onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}};
document.querySelectorAll("[data-text]").forEach(button=>button.onclick=()=>{$("messageInput").value=button.dataset.text;resize();$("messageInput").focus()});
$("previewButton").onclick=()=>preview(); $("finishButton").onclick=finish; $("saveFromPreview").onclick=finish; $("closePreview").onclick=closePreview; $("overlay").onclick=closePreview;
$("refreshDraft").onclick=()=>preview(true);$("saveDraft").onclick=saveDraft;
$("editDraft").onclick=()=>{
  const editor=$("draftEditor"),start=state.editorVisited?editor.selectionStart:0,end=state.editorVisited?editor.selectionEnd:0,scroll=state.editorVisited?editor.scrollTop:0;
  state.draftMode="edit";showDraft();editor.focus({preventScroll:true});editor.setSelectionRange(start,end);editor.scrollTop=scroll;state.editorVisited=true;
};
$("renderDraft").onclick=()=>{state.draftMode="read";showDraft()};
$("draftEditor").oninput=()=>{if(state.draft){state.draft.markdown=$("draftEditor").value;updateDraftControls()}};
window.addEventListener("beforeunload",event=>{if(draftDirty()){event.preventDefault();event.returnValue=""}});
$("themeButton").onclick=()=>theme(document.documentElement.dataset.theme==="dark"?"light":"dark"); $("mobileMenu").onclick=()=>$("sidebar").classList.toggle("open");
$("chatNav").onclick=()=>setView("chat");$("reviewNav").onclick=()=>setView("review");$("previousMonth").onclick=()=>moveMonth(-1);$("nextMonth").onclick=()=>moveMonth(1);
updateDraftControls();theme(localStorage.getItem("diary-theme")||"light"); bootstrap().catch(error=>showToast(error.message));
