"use strict";
const CACHE='tb-deck-v2';
const SHELL=['/','/app.css','/app.js','/deck.js','/manifest.webmanifest','/brand-mark.png','/brand-full.png','/apple-touch-icon.png','/icon-192.png'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL))));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('tb-')&&k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',event=>{const url=new URL(event.request.url);if(event.request.method!=='GET'||url.origin!==self.location.origin||!SHELL.includes(url.pathname))return;event.respondWith(fetch(event.request).catch(()=>caches.match(url.pathname)));});
// Cache application assets only. Never cache tokens, account API responses, or commands.
