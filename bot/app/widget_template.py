"""Immutable, network-free Telegram profile-1 widget. Data is server-owned."""

import json
import zlib


def encode_state(data):
    value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(value.encode()) > 1024:
        raise ValueError("Widget state exceeds 1024 bytes")
    return value


WIDGET_HTML = """<style>
body{margin:0;padding:12px;color:var(--tg-theme-text-color);font-family:system-ui}
header{border-left:4px solid var(--tg-theme-button-color);padding-left:10px;margin-bottom:12px}
h2{font-size:19px;margin:0}small,footer{color:var(--tg-theme-hint-color);font-size:12px}
.metrics{display:flex;gap:6px;margin-bottom:10px}.metric{flex:1;padding:8px;border-radius:10px;background:var(--tg-theme-secondary-bg-color)}
strong{display:block;font-size:19px}.row{padding:7px 0;border-bottom:1px solid var(--tg-theme-section-separator-color);display:flex;justify-content:space-between;gap:8px}
.row span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.row b{white-space:nowrap;font-size:13px}
.listrow{box-sizing:border-box;margin:0;display:grid;grid-template-columns:22px minmax(0,1fr) 28px 46px;gap:4px;align-items:center;width:100%;height:29px;min-height:29px;padding:3px 4px;border-radius:0;border:0;border-bottom:1px solid var(--tg-theme-section-separator-color);text-align:left;background:transparent;color:var(--tg-theme-text-color);font-size:12px}.listrow span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.listrow span:first-child{color:var(--tg-theme-hint-color)}nav{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px}button{min-height:36px;font-size:13px;border-radius:9px}
footer{margin-top:8px;min-height:16px}#status{font-size:12px;color:var(--tg-theme-hint-color)}
</style><header><small>CLAN COMMAND · ШТАБ</small><h2 id="title"></h2><small id="sub"></small></header><div id="metrics" class="metrics"></div><div id="rows"></div><small id="head"></small><div id="list"></div><nav id="nav"></nav><footer id="foot"></footer><div id="status"></div><script>
const W=TelegramWidget;let busy=false;
function el(t,s){let e=document.createElement(t);e.textContent=s==null?'':String(s);return e}
function clear(id){let e=document.getElementById(id);while(e.firstChild)e.removeChild(e.firstChild);return e}
function render(){let d;try{d=JSON.parse(W.chatData||'{}')}catch(e){return}
document.getElementById('title').textContent=d.t||'Штаб';document.getElementById('sub').textContent=d.s||'';
let k=clear('metrics');(d.k||[]).forEach(x=>{let a=el('div','');a.className='metric';a.appendChild(el('small',x[0]));a.appendChild(el('strong',x[1]));k.appendChild(a)});
let r=clear('rows');(d.r||[]).forEach(x=>{let a=el('div','');a.className='row';let b=el('span',x[0]);b.appendChild(el('br',''));b.appendChild(el('small',x[1]));a.appendChild(b);a.appendChild(el('b',x[2]));r.appendChild(a)});
document.getElementById('head').textContent=d.h||'';
function request(a){if(busy)return;busy=true;let p=W.request(d.n+':'+d.q+':'+a);render();document.getElementById('status').textContent='Загрузка…';p.then(x=>{document.getElementById('status').textContent=x.result||''}).catch(()=>{document.getElementById('status').textContent='Повтори нажатие или открой /classic.'}).finally(()=>{busy=false;render()})}
let list=clear('list');(d.l||[]).forEach((x,i)=>{let a=el(x[3]?'button':'div','');a.className='listrow';[String((d.o||0)+i+1),x[0],x[1],x[2]].forEach(v=>a.appendChild(el('span',v)));if(x[3]){a.disabled=busy;a.onclick=()=>request(x[3])}list.appendChild(a)});
let n=clear('nav');(d.b||[]).forEach(x=>{let b=el('button',x[0]);b.disabled=busy;b.onclick=()=>request(x[1]);n.appendChild(b)});document.getElementById('foot').textContent=d.f||''}
W.onEvent('chatDataChanged',render);render();W.ready();
</script>"""
assert len(WIDGET_HTML.encode()) <= 32768
assert len(zlib.compress(WIDGET_HTML.encode(), 9)) <= 4096
