"use strict";
// A 303 redirect inherits the fragment of the submitted form URL.
// Only the local detail route is carried through the native login.
if (/^#(radio|incident)\/[0-9a-f]{32}$/.test(location.hash)) {
  document.querySelector("form").action = "/auth/login" + location.hash;
}
