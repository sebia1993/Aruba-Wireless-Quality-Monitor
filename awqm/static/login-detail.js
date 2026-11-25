"use strict";
// A 303 redirect inherits the fragment of the submitted form URL.
// Only the local detail route is carried through the native login.
function syncDetailDestination(){
  const route=/^#(radio|incident|client)\/[0-9a-f]{32}$/.test(location.hash)?location.hash:"";
  document.querySelector("form").action="/auth/login"+route;
}
syncDetailDestination();
window.addEventListener("hashchange",syncDetailDestination);
