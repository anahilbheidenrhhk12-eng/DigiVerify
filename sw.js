const CACHE="dv-control-static-v1";
self.addEventListener("install",event=>{event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(["/icon.svg","/icon-192.png","/icon-512.png","/manifest.webmanifest"])));self.skipWaiting()});
self.addEventListener("activate",event=>{event.waitUntil(self.clients.claim())});
self.addEventListener("fetch",event=>{
 const url=new URL(event.request.url);
 // Never cache authenticated pages, API results, secrets, or session cookies.
 if(url.origin!==self.location.origin || url.pathname.startsWith("/api/") || url.pathname==="/app" || url.pathname==="/")return;
 if(event.request.method!=="GET")return;
 event.respondWith(caches.match(event.request).then(cached=>cached||fetch(event.request)));
});
