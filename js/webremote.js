var timer = null;
var lasttime = null;
var dragging = false; // user is moving the seekbar thumb
var lastposition = 0.0;

var streaming = false;
var needsync = false;
var audiotag = null;
var s = null;
var everConnected = false;

var accesskey = '';

// currently playing entry, as reported by the plugin
var currentId = 0;
var isPlaying = false;

// volume state tracking
var currentVolumeLevel = 'full'; // enum: 'muted', 'low', 'medium', 'full'
var volumeLevels = {
  muted: 0.0,
  low: 0.4,
  medium: 0.7,
  full: 1.0
};
var volumeCycle = ['muted', 'low', 'medium', 'full'];

// playback mode
var shuffleOn = false;
var repeatMode = 'off'; // 'off', 'all', 'one'
var repeatCycle = ['off', 'all', 'one'];

// library search / listing state
var SEARCH_PAGE_SIZE = 50;
var SEARCH_DEBOUNCE_MS = 200;
var search = {
  query: '',
  seq: 0,
  offset: 0,
  total: 0,
  loading: false,
  done: false,
  debounce: null,
  sort: null,   // column picked in the header; null = library order
  order: 'asc'
};

var sign = function (path) {
  var sh = new SipHash();
  var ts = new Date().getTime();
  var message = path + "\n" + ts;
  return "sig=" + sh.hash_hex(sh.string_to_key(accesskey), message) + "&ts=" + ts;
};

var pad = function (str, len, chr, left) {
  var padding = (str.length >= len) ? '' : new Array(1 + len - str.length >>> 0).join(chr);
  return left ? str + padding : padding + str;
};

var timestr = function (s) {
  var h = Math.floor(s / (60 * 60));
  var m = Math.floor((s % (60 * 60)) / 60);
  var sec = Math.floor(s % 60);
  if (h >= 1) {
    return h.toString() + ":" +
      pad(m.toString(), 2, '0', false) + ":" +
      pad(sec.toString(), 2, '0', false);
  } else {
    return m.toString() + ":" + pad(sec.toString(), 2, '0', false);
  }
};

var $ = function (id) {
  return document.getElementById(id);
};

var svgIcon = function (name, className) {
  var ns = "http://www.w3.org/2000/svg";
  var svg = document.createElementNS(ns, "svg");
  if (className) {
    svg.setAttribute("class", className);
  }
  var use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#" + name);
  svg.appendChild(use);
  return svg;
};

var setIcon = function (button, name) {
  button.querySelector("use").setAttribute("href", "#" + name);
};

// ---------- playback position ----------

var setPosition = function (ms) {
  var range = $("seekbar-range");
  range.value = ms;
  var max = parseFloat(range.max) || 0;
  var pct = max > 0 ? Math.min(100, (ms / max) * 100) : 0;
  range.style.setProperty("--progress", pct + "%");
  $("trackposition").textContent = timestr(ms / 1000);
};

var tick = function () {
  var now = new Date();
  var elapsed = (now.getTime() - lasttime.getTime());
  lasttime = now;

  // keep counting while the user drags, but don't move the thumb under them
  lastposition = lastposition + elapsed;
  if (!dragging) {
    setPosition(lastposition);
  }

  if (streaming && needsync) {
    audioTimeSync();
  }
};

// ---------- optional local audio streaming ----------

var audioCanSeekTo = function (seektime) {
  for (var i = 0; i < audiotag.seekable.length; i++) {
    if (audiotag.seekable.start(i) > seektime) {
      return false;
    }
    if (audiotag.seekable.end(i) > seektime) {
      return true;
    }
  }
  return false;
};

var audioTimeSync = function () {
  if (needsync) {
    if (audioCanSeekTo(lastposition / 1000)) {
      audiotag.currentTime = lastposition / 1000;

      if (timer != null) {
        audiotag.play();
      }
      needsync = false;
    }
  }
};

var createAudioTag = function () {
  audiotag = document.createElement("audio");
  // plugin should give us direct stream urls if they exist
  // and should tell us if the thing can't be streamed (cdda etc.) too
  var path = "/entry/current/stream";
  audiotag.setAttribute("src", path + "?" + sign(path));
  audiotag.setAttribute("preload", "auto");

  audiotag.addEventListener('canplay', audioTimeSync);
  needsync = true;

  var c = $("stream-container");
  while (c.firstChild) {
    c.removeChild(c.firstChild);
  }
  c.appendChild(audiotag);
};

var toggleStreaming = function () {
  if (streaming) {
    streaming = false;
    audiotag.pause();
    $("stream-container").removeChild(audiotag);
    audiotag = null;
  } else {
    streaming = true;
    createAudioTag();
  }
};

// ---------- album art ----------

var artUrl = function (albumart) {
  var path = "/art/" + albumart;
  return path + "?" + sign(path);
};

// fill a container with album art, or a music note when there is none
var setArt = function (container, albumart, placeholderClass) {
  while (container.firstChild) {
    container.removeChild(container.firstChild);
  }
  if (albumart) {
    var img = document.createElement("img");
    img.alt = "";
    img.decoding = "async";
    img.src = artUrl(albumart);
    img.onerror = function () {
      setArt(container, null, placeholderClass);
    };
    container.appendChild(img);
  } else {
    container.appendChild(svgIcon("i-note", placeholderClass));
  }
};

// ---------- connection ----------

var connectionState = function (connected) {
  $("connectform").hidden = connected;
  $("player-interface").hidden = !connected;
};

var getVolumeLevelFromValue = function (volume) {
  // convert numeric volume to enum level with tolerance
  if (volume < 0.05) return 'muted';
  if (volume < 0.55) return 'low';
  if (volume < 0.85) return 'medium';
  return 'full';
};

var updateVolumeUI = function (volume) {
  currentVolumeLevel = getVolumeLevelFromValue(volume);

  // presets read "Muted", "40%", "70%", "100%"; a volume set in Rhythmbox
  // itself shows its real percentage
  var percent = Math.round(volume * 100);
  var label = percent === 0 ? "Muted" : percent + "%";

  var buttons = document.querySelectorAll("[data-volume]");
  for (var i = 0; i < buttons.length; i++) {
    setIcon(buttons[i], "i-vol-" + currentVolumeLevel);
    buttons[i].querySelector(".volume-label").textContent = label;
    buttons[i].title = "Volume: " + label + " (click to change)";
    buttons[i].setAttribute("aria-label", "Volume: " + label);
  }
};

var setPlaying = function (playing) {
  isPlaying = playing;
  var btn = $("playpause");
  setIcon(btn, playing ? "i-pause" : "i-play");
  btn.setAttribute("aria-label", playing ? "Pause" : "Play");
  $("player-interface").classList.toggle("is-playing", playing);
};

var updateTrackSubtitle = function () {
  var hasArtist = $("trackartist").textContent !== "";
  var hasAlbum = $("trackalbum").textContent !== "";
  $("trackdot").hidden = !(hasArtist && hasAlbum);
};

var markCurrentSong = function () {
  var rows = $("song-list").children;
  for (var i = 0; i < rows.length; i++) {
    var current = parseInt(rows[i].dataset.id, 10) === currentId;
    rows[i].classList.toggle("is-current", current);
    if (current) {
      rows[i].setAttribute("aria-current", "true");
    } else {
      rows[i].removeAttribute("aria-current");
    }
  }
};

var send = function (msg) {
  if (s && s.readyState === WebSocket.OPEN) {
    s.send(JSON.stringify(msg));
  }
};

var connect = function () {
  accesskey = $("accesskey").value;
  everConnected = false;
  $("connect-error").hidden = true;

  var loc = document.location.hostname + ":" + document.location.port;
  s = new WebSocket("ws://" + loc + "/ws/player:" + sign("/ws/player"));

  s.onopen = function (event) {
    everConnected = true;
    connectionState(true);
    send({ "action": "status" });
    newSearch($("search-input").value);
  };

  s.onclose = function (event) {
    $("connectionhostname").textContent = "";
    window.clearInterval(timer);
    timer = null;
    lasttime = null;

    var error = $("connect-error");
    error.textContent = everConnected ?
      "Connection to Rhythmbox was lost. Connect again to continue." :
      "Couldn't connect. Check the passphrase and try again.";
    error.hidden = false;
    connectionState(false);
  };

  s.onmessage = function (event) {
    var m = JSON.parse(event.data);

    if ("search" in m) {
      handleSearchResults(m.search);
      return;
    }

    if ("shutdown" in m) {
      if (streaming) {
        toggleStreaming();
      }
    }

    if ("hostname" in m) {
      $("connectionhostname").textContent = m.hostname;
    }

    if ("id" in m) {
      lastposition = 0;
      setPosition(0);
      currentId = m.id;
      markCurrentSong();

      if (m.id === 0) {
        document.title = "Rhythmbox Remote";
        $("tracktitle").textContent = "Not Playing";
        $("trackartist").textContent = "";
        $("trackalbum").textContent = "";
        updateTrackSubtitle();
        setArt($("trackimage"), null, "art-placeholder");
      }

      if (streaming) {
        createAudioTag();
      }
    }

    if ("title" in m) {
      document.title = m.title || "Rhythmbox Remote";
      $("tracktitle").textContent = m.title || "Unknown Title";
    }

    if ("artist" in m) {
      $("trackartist").textContent = m.artist || "";
      updateTrackSubtitle();
    }

    if ("album" in m) {
      $("trackalbum").textContent = m.album || "";
      updateTrackSubtitle();
    }

    if ("duration" in m) {
      var range = $("seekbar-range");
      range.max = m.duration * 1000;
      range.min = 0;
      $("trackduration").textContent = timestr(m.duration);
      if (!dragging) {
        setPosition(lastposition);
      }
    }

    if ("playing" in m) {
      setPlaying(m.playing);
      if (m.playing) {
        if (timer == null) {
          timer = window.setInterval(tick, 250);
          lasttime = new Date();
        }
        if (streaming) {
          audiotag.play();
        }
      } else {
        window.clearInterval(timer);
        timer = null;
        lasttime = null;
        if (streaming) {
          audiotag.pause();
        }
      }
    }

    if ("position" in m) {
      lastposition = m.position;
      if (!dragging) {
        setPosition(m.position);
      }

      if (streaming) {
        needsync = true;
        audioTimeSync();
      }
    }

    if ("albumart" in m) {
      setArt($("trackimage"), m.albumart, "art-placeholder");
    }

    if ("volume" in m) {
      updateVolumeUI(m.volume);
    }

    if ("shuffle" in m || "repeat" in m) {
      updatePlayModeUI("shuffle" in m ? m.shuffle : shuffleOn,
                       "repeat" in m ? m.repeat : repeatMode);
    }
  };

  return false;
};

// ---------- library search ----------

var setListStatus = function (node) {
  var status = $("list-status");
  while (status.firstChild) {
    status.removeChild(status.firstChild);
  }
  if (node) {
    status.appendChild(node);
  }
};

var spinner = function () {
  var span = document.createElement("span");
  span.className = "spinner";
  span.setAttribute("role", "progressbar");
  span.setAttribute("aria-label", "Loading");
  return span;
};

var emptyState = function (text) {
  var div = document.createElement("div");
  div.className = "empty-state";
  div.appendChild(svgIcon("i-search"));
  div.appendChild(document.createTextNode(text));
  return div;
};

var requestPage = function () {
  search.loading = true;
  send({
    'action': 'search',
    'query': search.query,
    'offset': search.offset,
    'limit': SEARCH_PAGE_SIZE,
    'sort': search.sort,
    'order': search.order,
    'seq': search.seq
  });
};

var newSearch = function (query) {
  window.clearTimeout(search.debounce);
  search.query = query.trim();
  search.seq += 1;
  search.offset = 0;
  search.total = 0;
  search.done = false;

  var list = $("song-list");
  while (list.firstChild) {
    list.removeChild(list.firstChild);
  }
  $("library").scrollTop = 0;
  $("result-count").textContent = search.query ? "Searching…" : "Loading library…";
  setListStatus(spinner());
  requestPage();
};

var loadMore = function () {
  if (search.loading || search.done) {
    return;
  }
  setListStatus(spinner());
  requestPage();
};

var playSong = function (id) {
  send({ 'action': 'play', 'id': id });
};

var buildSongRow = function (song) {
  var li = document.createElement("li");
  li.className = "song";
  li.dataset.id = song.id;
  li.tabIndex = 0;
  li.setAttribute("role", "button");

  var art = document.createElement("div");
  art.className = "song-art";
  var slot = document.createElement("div");
  slot.className = "song-art-slot";
  setArt(slot, song.albumart, "");
  art.appendChild(slot);
  var eq = document.createElement("div");
  eq.className = "eq";
  eq.setAttribute("aria-hidden", "true");
  for (var i = 0; i < 3; i++) {
    eq.appendChild(document.createElement("span"));
  }

  var text = document.createElement("div");
  text.className = "song-text";
  var title = document.createElement("div");
  title.className = "song-title";
  title.textContent = song.title || "Unknown Title";
  var subtitle = document.createElement("div");
  subtitle.className = "song-subtitle";
  subtitle.textContent = song.artist || "Unknown Artist";
  text.appendChild(title);
  text.appendChild(subtitle);

  var artist = document.createElement("div");
  artist.className = "song-artist";
  artist.textContent = song.artist || "Unknown Artist";

  var album = document.createElement("div");
  album.className = "song-album";
  album.textContent = song.album || "";

  var year = document.createElement("div");
  year.className = "song-year";
  year.textContent = song.year ? song.year : "";

  var duration = document.createElement("div");
  duration.className = "song-duration";
  duration.textContent = song.duration ? timestr(song.duration) : "";

  li.appendChild(art);
  li.appendChild(text);
  li.appendChild(artist);
  li.appendChild(album);
  li.appendChild(year);
  li.appendChild(duration);
  art.appendChild(eq);

  li.setAttribute("aria-label",
    (song.title || "Unknown Title") + ", " + (song.artist || "Unknown Artist"));
  return li;
};

var handleSearchResults = function (r) {
  if (r.seq !== search.seq) {
    return; // response to an outdated query
  }
  search.loading = false;

  if (r.error) {
    $("result-count").textContent = "Search failed";
    setListStatus(document.createTextNode(r.error));
    return;
  }

  var list = $("song-list");
  var frag = document.createDocumentFragment();
  for (var i = 0; i < r.results.length; i++) {
    frag.appendChild(buildSongRow(r.results[i]));
  }
  list.appendChild(frag);
  markCurrentSong();

  search.total = r.total;
  search.offset = r.offset + r.results.length;
  search.done = search.offset >= r.total || r.results.length === 0;

  var count = r.total.toLocaleString();
  var noun = r.total === 1 ? " song" : " songs";
  $("result-count").textContent = search.query ?
    count + (r.total === 1 ? " result" : " results") : count + noun;

  if (r.total === 0) {
    setListStatus(emptyState(search.query ?
      "No songs match “" + search.query + "”" :
      "Your library is empty"));
  } else {
    setListStatus(null);
    // the sentinel may still be visible (short page, tall screen): keep loading
    if (!search.done && sentinelVisible()) {
      loadMore();
    }
  }
};

var sentinelVisible = function () {
  var lib = $("library").getBoundingClientRect();
  var sentinel = $("list-sentinel").getBoundingClientRect();
  return sentinel.top < lib.bottom + 600;
};

var onSearchInput = function () {
  var value = $("search-input").value;
  $("search-clear").hidden = value === "";
  window.clearTimeout(search.debounce);
  search.debounce = window.setTimeout(function () {
    if (value.trim() !== search.query) {
      newSearch(value);
    }
  }, SEARCH_DEBOUNCE_MS);
};

// header click: sort by that column ascending; click again to flip the order
var sortBy = function (column) {
  if (search.sort === column) {
    search.order = search.order === 'asc' ? 'desc' : 'asc';
  } else {
    search.sort = column;
    search.order = 'asc';
  }
  updateSortHeaders();
  newSearch($("search-input").value);
};

var updateSortHeaders = function () {
  var buttons = document.querySelectorAll(".col-sort");
  for (var i = 0; i < buttons.length; i++) {
    var b = buttons[i];
    var active = b.dataset.sort === search.sort;
    var name = b.textContent.trim();
    b.classList.toggle("is-active", active);
    b.classList.toggle("is-desc", active && search.order === 'desc');
    b.setAttribute("aria-pressed", active ? "true" : "false");
    b.setAttribute("aria-label", active ?
      "Sorted by " + name + (search.order === 'asc' ? ", ascending" : ", descending") :
      "Sort by " + name);
    b.title = active ?
      (search.order === 'asc' ? "Ascending: click for descending" : "Descending: click for ascending") :
      "Sort by " + name;
  }
};

var clearSearch = function () {
  var input = $("search-input");
  input.value = "";
  $("search-clear").hidden = true;
  if (search.query !== "") {
    newSearch("");
  }
  input.focus();
};

// ---------- controls ----------

var sendevent = function () {
  send({ 'action': this.id });
};

// while dragging, only preview the new time
var seekinput = function () {
  dragging = true;
  setPosition(parseFloat(this.value));
};

// on release (or a click / arrow key), seek once to the chosen point
var seekchange = function () {
  dragging = false;
  lastposition = parseFloat(this.value);
  setPosition(lastposition);
  send({ 'action': 'seek', 'time': Math.floor(lastposition / 1000) });
};

var volumeToggle = function () {
  // cycle through: muted -> low -> medium -> full -> muted
  var currentIndex = volumeCycle.indexOf(currentVolumeLevel);
  var nextIndex = (currentIndex + 1) % volumeCycle.length;
  var nextVolume = volumeLevels[volumeCycle[nextIndex]];

  // update UI immediately
  updateVolumeUI(nextVolume);

  // send to backend
  send({ 'action': 'volume', 'volume': nextVolume });
};

var updatePlayModeUI = function (shuffle, repeat) {
  shuffleOn = shuffle;
  repeatMode = repeat;

  var sb = $("shuffle");
  var shuffleLabel = "Shuffle: " + (shuffle ? "on" : "off");
  sb.classList.toggle("is-on", shuffle);
  sb.setAttribute("aria-pressed", shuffle ? "true" : "false");
  sb.setAttribute("aria-label", shuffleLabel);
  sb.title = shuffleLabel;

  var rb = $("repeat");
  var repeatLabel = "Repeat: " + { off: "off", all: "all songs", one: "this song" }[repeat];
  rb.dataset.mode = repeat;
  rb.classList.toggle("is-on", repeat !== 'off');
  setIcon(rb, repeat === 'one' ? "i-repeat-one" : "i-repeat");
  rb.setAttribute("aria-label", repeatLabel);
  rb.title = repeatLabel;
};

var shuffleToggle = function () {
  updatePlayModeUI(!shuffleOn, repeatMode);
  send({ 'action': 'playmode', 'shuffle': shuffleOn });
};

// off -> all -> one -> off
var repeatToggle = function () {
  var next = repeatCycle[(repeatCycle.indexOf(repeatMode) + 1) % repeatCycle.length];
  updatePlayModeUI(shuffleOn, next);
  send({ 'action': 'playmode', 'repeat': next });
};

var isTyping = function (el) {
  return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
};

window.onload = function () {
  // main controls
  $("previous").addEventListener('click', sendevent);
  $("playpause").addEventListener('click', sendevent);
  $("next").addEventListener('click', sendevent);
  $("shuffle").addEventListener('click', shuffleToggle);
  $("repeat").addEventListener('click', repeatToggle);

  // seekbar
  $("seekbar-range").addEventListener('input', seekinput);
  $("seekbar-range").addEventListener('change', seekchange);

  // volume control (one button on mobile, one on desktop)
  var volumeButtons = document.querySelectorAll("[data-volume]");
  for (var i = 0; i < volumeButtons.length; i++) {
    volumeButtons[i].addEventListener('click', volumeToggle);
  }

  // search
  var input = $("search-input");
  input.addEventListener('input', onSearchInput);
  input.addEventListener('keydown', function (e) {
    if (e.key === "Escape") {
      clearSearch();
    } else if (e.key === "Enter") {
      window.clearTimeout(search.debounce);
      if (input.value.trim() !== search.query) {
        newSearch(input.value);
      }
      input.blur(); // hide the on-screen keyboard on phones
    }
  });
  $("search-clear").addEventListener('click', clearSearch);

  // column header sorting (desktop layout)
  $("list-columns").addEventListener('click', function (e) {
    var b = e.target.closest(".col-sort");
    if (b) {
      sortBy(b.dataset.sort);
    }
  });
  updateSortHeaders();

  // song list: click or Enter/Space on a row plays it
  var list = $("song-list");
  list.addEventListener('click', function (e) {
    var row = e.target.closest(".song");
    if (row) {
      playSong(parseInt(row.dataset.id, 10));
    }
  });
  list.addEventListener('keydown', function (e) {
    var row = e.target.closest(".song");
    if (row && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      playSong(parseInt(row.dataset.id, 10));
    }
  });

  // infinite scroll
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting) {
        loadMore();
      }
    }, { root: $("library"), rootMargin: "600px 0px" }).observe($("list-sentinel"));
  } else {
    $("library").addEventListener('scroll', function () {
      if (sentinelVisible()) {
        loadMore();
      }
    });
  }

  // keyboard shortcuts: "/" focuses search, space toggles playback
  document.addEventListener('keydown', function (e) {
    if ($("player-interface").hidden || isTyping(document.activeElement) ||
        e.ctrlKey || e.metaKey || e.altKey) {
      return;
    }
    if (e.key === "/") {
      e.preventDefault();
      input.focus();
    } else if (e.key === " " && !e.target.closest("button, .song, input")) {
      e.preventDefault();
      send({ 'action': 'playpause' });
    }
  });

  // connection form
  $("connect-form").onsubmit = connect;
  $("accesskey").focus();
};

// siphash implementation from https://github.com/jedisct1/siphash-js

function SipHash() {
  function _add(a, b) {
    var rl = a.l + b.l,
      a2 = {
        h: a.h + b.h + (rl / 2 >>> 31) >>> 0,
        l: rl >>> 0
      };
    a.h = a2.h; a.l = a2.l;
  }

  function _xor(a, b) {
    a.h ^= b.h; a.h >>>= 0;
    a.l ^= b.l; a.l >>>= 0;
  }

  function _rotl(a, n) {
    var a2 = {
      h: a.h << n | a.l >>> (32 - n),
      l: a.l << n | a.h >>> (32 - n)
    };
    a.h = a2.h; a.l = a2.l;
  }

  function _rotl32(a) {
    var al = a.l;
    a.l = a.h; a.h = al;
  }

  function _compress(v0, v1, v2, v3) {
    _add(v0, v1);
    _add(v2, v3);
    _rotl(v1, 13);
    _rotl(v3, 16);
    _xor(v1, v0);
    _xor(v3, v2);
    _rotl32(v0);
    _add(v2, v1);
    _add(v0, v3);
    _rotl(v1, 17);
    _rotl(v3, 21);
    _xor(v1, v2);
    _xor(v3, v0);
    _rotl32(v2);
  }

  function _get_int(a, offset) {
    return a.charCodeAt(offset + 3) << 24 |
      a.charCodeAt(offset + 2) << 16 |
      a.charCodeAt(offset + 1) << 8 |
      a.charCodeAt(offset);
  }

  function hash(key, m) {
    var k0 = { h: key[1] >>> 0, l: key[0] >>> 0 },
      k1 = { h: key[3] >>> 0, l: key[2] >>> 0 },
      v0 = { h: k0.h, l: k0.l }, v2 = k0,
      v1 = { h: k1.h, l: k1.l }, v3 = k1,
      mi, mp = 0, ml = m.length, ml7 = ml - 7,
      buf = new Uint8Array(new ArrayBuffer(8));

    _xor(v0, { h: 0x736f6d65, l: 0x70736575 });
    _xor(v1, { h: 0x646f7261, l: 0x6e646f6d });
    _xor(v2, { h: 0x6c796765, l: 0x6e657261 });
    _xor(v3, { h: 0x74656462, l: 0x79746573 });
    while (mp < ml7) {
      mi = { h: _get_int(m, mp + 4), l: _get_int(m, mp) };
      _xor(v3, mi);
      _compress(v0, v1, v2, v3);
      _compress(v0, v1, v2, v3);
      _xor(v0, mi);
      mp += 8;
    }
    buf[7] = ml;
    var ic = 0;
    while (mp < ml) {
      buf[ic++] = m.charCodeAt(mp++);
    }
    while (ic < 7) {
      buf[ic++] = 0;
    }
    mi = {
      h: buf[7] << 24 | buf[6] << 16 | buf[5] << 8 | buf[4],
      l: buf[3] << 24 | buf[2] << 16 | buf[1] << 8 | buf[0]
    };
    _xor(v3, mi);
    _compress(v0, v1, v2, v3);
    _compress(v0, v1, v2, v3);
    _xor(v0, mi);
    _xor(v2, { h: 0, l: 0xff });
    _compress(v0, v1, v2, v3);
    _compress(v0, v1, v2, v3);
    _compress(v0, v1, v2, v3);
    _compress(v0, v1, v2, v3);

    var h = v0;
    _xor(h, v1);
    _xor(h, v2);
    _xor(h, v3);

    return h;
  }

  function string_to_key(a) {
    var k = [0, 0, 0, 0];
    var i = 0, ki = 0;

    pa = a + "\0\0\0\0";
    while (i < a.length) {
      k[ki] = (k[ki] + _get_int(pa, i)) % 0xffffffff;
      i += 4;
      ki = (ki + 1) % 4;
    }

    return k;
  }

  function hash_hex(key, m) {
    var r = hash(key, m);
    return ("0000000" + r.h.toString(16)).substr(-8) +
      ("0000000" + r.l.toString(16)).substr(-8);
  }

  function hash_uint(key, m) {
    var r = hash(key, m);
    return (r.h & 0x1fffff) * 0x100000000 + r.l;
  }

  return {
    string_to_key: string_to_key,
    hash: hash,
    hash_hex: hash_hex,
    hash_uint: hash_uint
  };
};
