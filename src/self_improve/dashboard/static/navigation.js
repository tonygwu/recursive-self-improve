/* Deterministic navigation only. The host supplies existing GET readers and routes. */
const STORAGE_KEY = "self-improve.navigation.v1";
const LIMIT = 10;
const LABELS = {learning:"Rule",proposal:"Proposal",incident:"Incident",session:"Session",instruction:"Instruction file",revision:"Delivered revision"};
export const NAVIGATION_PAGES = Object.freeze([
  {label:"Overview",href:"#/overview",detail:"Loop health and exact run history"},
  {label:"Rules",href:"#/rules",detail:"Learned rules and families"},
  {label:"Review queue",href:"#/review",detail:"Decisions, delivery and recovery"},
  {label:"Projects",href:"#/projects",detail:"Working copies and instruction availability"},
  {label:"Evals & trends",href:"#/evals",detail:"Evaluation evidence, quality and policy"},
  {label:"Search all evidence",href:"#/rules?mode=evidence",detail:"Rules, proposals, incidents, sessions and instruction text"},
]);
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
export function isEditing(target) {
  return Boolean(target && (/^(INPUT|TEXTAREA|SELECT)$/i.test(target.tagName || "") || target.isContentEditable || target.closest?.('[contenteditable]:not([contenteditable="false"])')));
}
export function safeNavigationLink(value) {
  if (!value || typeof value.href !== "string" || typeof value.label !== "string") return null;
  const {href,label}=value;
  if (href.length>8192 || !label.trim() || /[\u0000-\u001f\u007f]/.test(href)) return null;
  if (!/^#\/(?:overview(?:\/(?:run|night)\/[^?]+)?|rules(?:\/[^?]+)?|projects(?:\/[^?]+)?|evals(?:\/(?:attempt|unlinked|quality)\/[^?]+)?|review(?:\/(?:family|proposal|command|operation)\/[^?]+)?)(?:\?[^#]*)?$/.test(href)) return null;
  try {decodeURIComponent(href);} catch {return null;}
  return {href,label:label.slice(0,160)};
}
export function readSavedNavigation(storage) {
  try {
    const raw=storage?.getItem(STORAGE_KEY);
    if (!raw) return {favorites:[],recent:[],notice:storage ? "" : "Saved links last only for this session."};
    const parsed=JSON.parse(raw);
    if (parsed.version!==1 || !Array.isArray(parsed.favorites) || !Array.isArray(parsed.recent)) throw new Error("shape");
    const clean=items => [...new Map(items.map(safeNavigationLink).filter(Boolean).map(x=>[x.href,x])).values()].slice(0,LIMIT);
    return {favorites:clean(parsed.favorites),recent:clean(parsed.recent),notice:""};
  } catch {return {favorites:[],recent:[],notice:"Saved links could not be read. Changes will last for this session if browser storage is unavailable."};}
}
export function evidenceNavigation(page) {
  if (!page || !Array.isArray(page.rows) || !page.pagination || !Number.isSafeInteger(page.pagination.count) || page.pagination.count<page.rows.length || page.rows.length>8) throw new Error("The evidence reader returned an invalid result.");
  return page.rows.map(row => {
    if (!row || !Object.hasOwn(LABELS,row.kind) || typeof row.source_id!=="string" || !row.source_id || typeof row.title!=="string" || (row.excerpt!=null && typeof row.excerpt!=="string")) throw new Error("The evidence reader returned an invalid source.");
    return {href:`#/rules/evidence/${row.kind}/${encodeURIComponent(row.source_id)}?mode=evidence`,label:row.title,detail:LABELS[row.kind]+" · "+row.source_id,group:"Evidence"};
  });
}
export function directNavigation(query) {
  const match=/^(run|command|operation):\s*(\S+)$/i.exec(query.trim());
  if (!match) return [];
  const kind=match[1].toLowerCase(),id=match[2];
  return [{href:kind==="run"?`#/overview/run/${encodeURIComponent(id)}`:`#/review/${kind}/${encodeURIComponent(id)}`,label:`Open ${kind}: ${id}`,detail:"Read this exact record",group:"Exact record"}];
}
export function initNavigation({document:d,navigate,search,projects,current,toggleTheme,storage}) {
  const dialog=d.getElementById("navigation-dialog");
  // Importable in non-browser consumers; actual browsers use the native modal.
  if (!dialog?.showModal) return null;
  const input=d.getElementById("navigation-input"),list=d.getElementById("navigation-results"),status=d.getElementById("navigation-status"),savedStatus=d.getElementById("navigation-saved-status"),favorite=d.getElementById("navigation-favorite");
  const saved=readSavedNavigation(storage);
  let opener=null,query="",rows=[],active=0,remote=[],total=null,error="",loading=false,generation=0,timer=null,selectionTouched=false;
  function persist() {
    try {if(!storage)throw new Error("unavailable");storage.setItem(STORAGE_KEY,JSON.stringify({version:1,favorites:saved.favorites,recent:saved.recent}));saved.notice="";}
    catch {saved.notice="Browser storage is unavailable. Saved links last only for this session.";}
  }
  function record(value) {
    const link=safeNavigationLink(value);if(!link)return;
    saved.recent=[link,...saved.recent.filter(x=>x.href!==link.href)].slice(0,LIMIT);persist();
  }
  function choices() {
    const words=query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const matches=row=>words.every(word=>(row.label+" "+(row.detail||"")).toLocaleLowerCase().includes(word));
    const pages=NAVIGATION_PAGES.map(x=>({...x,group:"Go to"}));
    const commands=[{label:"Toggle theme",detail:"Switch light and dark appearance",action:"theme",group:"Actions"},{label:"Clear recent items",detail:"Forget locally saved recent navigation",action:"clear",group:"Actions"}];
    if(!query)return [...saved.favorites.map(x=>({...x,group:"Favorites"})),...saved.recent.filter(x=>!saved.favorites.some(y=>y.href===x.href)).map(x=>({...x,group:"Recent"})),...pages,...commands];
    const projectRows=(projects()?.rows || []).filter(p=>p.project_key).map(p=>({label:p.label || p.project_key,detail:p.project_key,href:`#/projects/${encodeURIComponent(p.project_key)}`,group:"Projects"})).filter(matches);
    return [...directNavigation(query),...pages.filter(matches),...commands.filter(matches),...projectRows,...remote,
      {label:`Search all evidence for “${query}”`,detail:total==null?"Open complete search":`${total} matching sources · show all results`,href:"#/rules?mode=evidence&query="+encodeURIComponent(query),group:"All results"}];
  }
  function paint({reset=false,keepIndex=false}={}) {
    if(!dialog.open)return;
    const selected=rows[active],key=selected?.href || selected?.action;
    rows=choices();
    const retained=rows.findIndex(x=>(x.href || x.action)===key);
    active=reset?0:Math.max(0,keepIndex?Math.min(active,rows.length-1):retained<0?Math.min(active,rows.length-1):retained);
    let group="";
    list.innerHTML=rows.map((row,i)=>{
      const heading=row.group!==group ? `<div class="navigation-group" role="presentation">${escape(row.group)}</div>` : "";group=row.group;
      return heading+`<div class="navigation-result" role="option" id="navigation-option-${i}" aria-selected="${i===active}" data-navigation-index="${i}"><span class="navigation-result__label">${escape(row.label)}</span><span class="navigation-result__detail">${escape(row.detail || row.href)}</span></div>`;
    }).join("");
    input.setAttribute("aria-activedescendant",rows.length?"navigation-option-"+active:"");
    list.setAttribute("aria-busy",String(loading));
    status.textContent=loading?"Searching retained evidence…":error?"Evidence search failed: "+error:query?`${total==null?"":total+" matching evidence sources. "}${rows.length} destinations. Arrow keys to move; Enter to open.`:"Pages, favorites and recent items. Arrow keys to move; Enter to open.";
    d.getElementById("navigation-retry").hidden=!error;
    savedStatus.textContent=saved.notice;
    d.getElementById("navigation-save-current").disabled=!safeNavigationLink(current());
    const row=rows[active],isSaved=row?.href && saved.favorites.some(x=>x.href===row.href);
    favorite.disabled=!row?.href;favorite.textContent=isSaved?"Remove favorite":"Save selected";
    favorite.setAttribute("aria-pressed",String(Boolean(isSaved)));
    d.getElementById("navigation-option-"+active)?.scrollIntoView({block:"nearest"});
  }
  function invalidate(){generation++;clearTimeout(timer);timer=null;}
  async function read(expected) {
    try {
      const page=await search(query);
      if(expected!==generation || !dialog.open)return;
      remote=evidenceNavigation(page);total=page.pagination.count;
    } catch(e){if(expected!==generation || !dialog.open)return;error=String(e.message || e);}
    finally {if(expected===generation && dialog.open){loading=false;paint({reset:!selectionTouched});}}
  }
  function update(immediate=false) {
    invalidate();selectionTouched=false;query=input.value.trim().slice(0,500).toWellFormed();remote=[];total=null;error="";loading=Boolean(query);paint({reset:true});
    if(query){const expected=generation;if(immediate)read(expected);else timer=setTimeout(()=>{timer=null;read(expected);},200);}
  }
  function open() {
    if(dialog.open){input.focus();return;}
    opener=d.activeElement;input.value="";query="";remote=[];error="";total=null;loading=false;active=0;
    dialog.showModal();paint({reset:true});input.focus();
  }
  function close(restore=true) {
    invalidate();if(!dialog.open)return;dialog.close();
    if(restore && opener?.isConnected)opener.focus({preventScroll:true});
  }
  function choose() {
    const row=rows[active];if(!row)return;
    if(row.action==="theme"){toggleTheme();close();return;}
    if(row.action==="clear"){saved.recent=[];persist();paint({reset:true});return;}
    const link=safeNavigationLink(row);if(!link)return;
    record(link);close(false);navigate(link.href);
  }
  const controller={open,close,record,handleShortcut(event){
    const chord=!event.isComposing && !event.altKey && (event.metaKey || event.ctrlKey) && event.key.toLowerCase()==="k";
    if(dialog.open){if(chord){event.preventDefault();input.focus();}return true;}
    if(event.isComposing || event.repeat || event.altKey || event.defaultPrevented)return false;
    if(chord) {event.preventDefault();open();return true;}
    return false;
  }};
  d.getElementById("navigation-open").addEventListener("click",open);
  d.getElementById("navigation-close").addEventListener("click",()=>close());
  d.getElementById("navigation-retry").addEventListener("click",()=>update(true));
  d.getElementById("navigation-save-current").addEventListener("click",()=>{
    const link=safeNavigationLink(current());if(!link)return;
    saved.favorites=[link,...saved.favorites.filter(x=>x.href!==link.href)].slice(0,LIMIT);persist();paint({reset:true});
  });
  favorite.addEventListener("click",()=>{
    selectionTouched=true;
    const link=safeNavigationLink(rows[active]);if(!link)return;
    saved.favorites=saved.favorites.some(x=>x.href===link.href)?saved.favorites.filter(x=>x.href!==link.href):[link,...saved.favorites].slice(0,LIMIT);persist();paint();
  });
  input.addEventListener("input",event=>{if(!event.isComposing)update();});
  input.addEventListener("compositionend",()=>update());
  list.addEventListener("click",event=>{const option=event.target.closest('[data-navigation-index]');if(option){active=Number(option.dataset.navigationIndex);choose();}});
  dialog.addEventListener("cancel",event=>{event.preventDefault();close();});
  dialog.addEventListener("click",event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left || event.clientX>r.right || event.clientY<r.top || event.clientY>r.bottom)close();}});
  dialog.addEventListener("keydown",event=>{
    if(event.isComposing || event.metaKey || event.ctrlKey || event.altKey)return;
    if(event.key==="Escape"){event.preventDefault();event.stopPropagation();close();return;}
    if(event.key==="Tab") {
      const stops=[...dialog.querySelectorAll('button:not([disabled]):not([hidden]), input:not([disabled])')];
      const first=stops[0],last=stops[stops.length-1];
      if(event.shiftKey && d.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey && d.activeElement===last){event.preventDefault();first.focus();}
      return;
    }
    if(event.target!==input)return;
    if(["ArrowDown","ArrowUp"].includes(event.key)){
      event.preventDefault();selectionTouched=true;active=(active+(event.key==="ArrowDown"?1:-1)+rows.length)%rows.length;paint({keepIndex:true});
    } else if(event.key==="Enter"){event.preventDefault();choose();}
  });
  return controller;
}
