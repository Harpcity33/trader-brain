'use strict';
// Presentation only: no order logic, provider calls, credentials, or extra timers for trading.
const reveal = document.getElementById('show-token');
const tokenInput = document.getElementById('pair-token');
reveal.addEventListener('click', () => {
  const showing = tokenInput.type === 'password';
  tokenInput.type = showing ? 'text' : 'password';
  reveal.textContent = showing ? 'Hide' : 'Show';
  reveal.setAttribute('aria-pressed', String(showing));
  reveal.setAttribute('aria-label', showing ? 'Hide pairing token' : 'Show pairing token');
});
const clock = document.getElementById('deck-clock');
const formatClock = new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
function clockTick(){const now=new Date();clock.textContent=formatClock.format(now);clock.dateTime=now.toISOString();}
clockTick();setInterval(clockTick,1000);
const navLinks = Array.from(document.querySelectorAll('.rail nav a'));
function selectNav(id){navLinks.forEach(link=>{const active=link.hash==='#'+id;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','location');else link.removeAttribute('aria-current');});}
navLinks.forEach(link=>link.addEventListener('click',()=>selectNav(link.hash.slice(1))));
if('IntersectionObserver' in window){
  const sections=['overview','positions','watchlist','review'].map(id=>document.getElementById(id));
  const observer=new IntersectionObserver(entries=>{const seen=entries.filter(e=>e.isIntersecting).sort((a,b)=>a.boundingClientRect.top-b.boundingClientRect.top);if(seen.length)selectNav(seen[0].target.id);},{rootMargin:'-12% 0px -55% 0px',threshold:0});
  sections.forEach(section=>observer.observe(section));
}
