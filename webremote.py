# -*- Mode: python; coding: utf-8; tab-width: 8; indent-tabs-mode: t; -*-
#
# Copyright (C) 2016 Jonathan Matthew
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# The Rhythmbox authors hereby grant permission for non-GPL compatible
# GStreamer plugins to be used and distributed together with GStreamer
# and Rhythmbox. This permission is above and beyond the permissions granted
# by the GPL license by which Rhythmbox is covered. If you modify this code
# you may extend this exception to your version of the code, but you are not
# obligated to do so. If you do not wish to do so, delete this exception
# statement from your version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA.

import gi
gi.require_version('Soup', '3.0')
from gi.repository import GLib, GObject, Gio, Peas, PeasGtk, Soup, Gtk
from gi.repository import RB
import rb

import sys
import os.path
import json
import re
import time
import struct
import unicodedata

import siphash

import gettext
gettext.install('rhythmbox', RB.locale_dir())

def get_host_name():
        try:
                p = Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SYSTEM,
                                                   0,
                                                   None,
                                                   'org.freedesktop.Avahi',
                                                   '/',
                                                   'org.freedesktop.Avahi.Server')
                return p.GetHostNameFqdn()
        except Exception as e:
                # ignore
                import socket
                return socket.gethostname()



class ClientSession(object):

	def __init__(self, plugin, connection, connid):
		print("new connection attached")
		self.connid = connid
		self.conn = connection 
		self.conn.connect("message", self.message_cb)
		self.conn.connect("closed", self.closed_cb)
		self.plugin = plugin
		self.actions = {
			'status': self.plugin.client_status,
			'next': self.plugin.client_next,
			'previous': self.plugin.client_previous,
			'playpause': self.plugin.client_playpause,
			'seek': self.plugin.client_seek,
			'volume': self.plugin.client_volume,
			'search': self.plugin.client_search,
			'play': self.plugin.client_play,
			'playmode': self.plugin.client_playmode
		}

	def message_cb(self, conn, msgtype, message):
		if msgtype != Soup.WebsocketDataType.TEXT:
			print("binary message received?")
			return

		d = message.get_data().decode("utf-8")
		print("message received: %s" % d)
		try:
			m = json.loads(d)
			action = m.get('action')
			print("doing %s" % action)
			if action in self.actions:
				r = self.actions[action](m)
			else:
				r = {'result': 'what'}

			print("responding %s" % str(r))
			self.conn.send_text(json.dumps(r))
		except Exception as e:
			sys.excepthook(*sys.exc_info())

	def closed_cb(self, conn):
		self.plugin.player_websocket_closed(self, self.connid)

	def dispatch(self, message):
		self.conn.send_text(message)

	def disconnect(self):
		self.conn.close(0, "")

class TrackStreamer(object):
	def __init__(self, server, message, track, content_type):
		self.server = server
		self.message = message
		self.message.connect("wrote-chunk", self.wrote_chunk)
		self.trackfile = Gio.File.new_for_uri(track)
		self.content_type = content_type
		self.stream = None
		self.offset = 0
		self.done = False
		print("streaming " + track)

	def wrote_chunk(self, msg):
		if not self.done:
			self.server.pause_message(self.message)

	def open(self):
		print("opening")
		self.trackfile.read_async(GLib.PRIORITY_DEFAULT, None, self.opened)
		self.server.pause_message(self.message)

	def opened(self, obj, result):
		try:
			print("track opened")
			headers = self.message.props.response_headers
			headers.set_content_type(self.content_type)
			headers.set_encoding(Soup.Encoding.CHUNKED)

			body = self.message.props.response_body
			body.set_accumulate(False)

			self.stream = self.trackfile.read_finish(result)
			self.message.set_status(200)
			self.read_more()
		except Exception as e:
			sys.excepthook(*sys.exc_info())
			self.message.set_status(500)
			self.server.unpause_message(self.message)

	def read_more(self):
		self.stream.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, self.read_done)

	def read_done(self, obj, result):
		body = self.message.props.response_body
		try:
			b = self.stream.read_bytes_finish(result)
			if b.get_size() == 0:
				self.done = True
				body.complete()
			else:
				self.offset = self.offset + b.get_size()
				body.append(b.get_data()) # uh..
				self.read_more()
			self.server.unpause_message(self.message)

		except Exception as e:
			sys.excepthook(*sys.exc_info())
			if (self.offset == 0):
				self.message.set_status(500)
			else:
				body.complete()
			self.server.unpause_message(self.message)



class WebRemotePlugin(GObject.Object, Peas.Activatable):
	__gtype_name = 'WebRemotePlugin'
	object = GObject.property(type=GObject.GObject)

	signature_max_age = 60
	# we don't really need a huge replay memory.
	# clients should only make requests every couple of minutes,
	# and signatures are only valid for (currently) up to two minutes.
	replay_memory = 20

	string_props = {
		RB.RhythmDBPropType.TITLE: 'title',
		RB.RhythmDBPropType.ARTIST: 'artist',
		RB.RhythmDBPropType.ALBUM: 'album',
		RB.RhythmDBPropType.ALBUM_ARTIST: 'album-artist',
		RB.RhythmDBPropType.GENRE: 'genre',
		RB.RhythmDBPropType.COMPOSER: 'composer',
		RB.RhythmDBPropType.TITLE: 'title',
	}
	ulong_props = {
		RB.RhythmDBPropType.ENTRY_ID: 'id',
		RB.RhythmDBPropType.YEAR: 'year',
		#RB.RhythmDBPropType.BEATS_PER_MINUTE: 'bpm',
		RB.RhythmDBPropType.BITRATE: 'bitrate',
		RB.RhythmDBPropType.DURATION: 'duration',
		RB.RhythmDBPropType.TRACK_NUMBER: 'track-number',
		RB.RhythmDBPropType.TRACK_TOTAL: 'track-total',
		RB.RhythmDBPropType.DISC_NUMBER: 'disc-number',
		RB.RhythmDBPropType.DISC_TOTAL: 'disc-total'
	}

	def __init__(self):
		GObject.Object.__init__(self)
		self.settings = Gio.Settings.new("org.gnome.rhythmbox.plugins.webremote")
		self.settings.connect("changed", self.settings_changed_cb)
		self.player_settings = Gio.Settings.new("org.gnome.rhythmbox.player")
		self.server = None
		self.next_connid = 0
		self.connections = {}
		self.replay = []
		self.access_key = None

		self.listen_reset = False
		self.library_cache = None
		self.sorted_cache = {}

		# Rhythmbox has no "repeat one" play order, so the plugin does it:
		# when a song finishes on its own, play it again
		self.repeat_one = False
		self.current_entry = None
		self.elapsed = 0

	def get_sign_key(self, id):
		# some day there will be multiple keys
		a = self.settings['access-key']

		ea = a.encode()
		pa = (a + 4 * '\0').encode()

		k = [0, 0, 0, 0]
		i = 0
		ki = 0
		uint = struct.Struct("<I")
		while i < len(ea):
			k[ki] = (k[ki] + uint.unpack(pa[i:i+4])[0]) % 0xffffffff
			i = i + 4
			ki = (ki + 1) % 4

		return k

	def check_http_signature(self, path, query):
		try:
			qargs = dict([b.split("=") for b in query.split("&")])
			ts = qargs['ts']
			sig = qargs['sig']
			keyid = qargs.get("k", "default")	# not used yet

			if sig in self.replay:
				print("replayed signature " + sig + " in request for " + path)
				return False

			its = int(ts) / 1000
			now = time.time()
			max = self.signature_max_age
			print("request timestamp: " + ts + ", min: " + str(now - max) + ", max: " + str(now + max))
			if (its < (now - max)) or (its > (now + max)):
				return False

			message = (path + "\n" + ts).encode()
			check = siphash.SipHash_2_4(self.get_sign_key(keyid), message).hexdigest()
			print("request signature: " + sig + ", expecting: " + check.decode())
			if check == sig.encode():
				self.replay.insert(0, sig)
				self.replay = self.replay[:self.replay_memory]
				return True

			return False

		except Exception as e:
			sys.excepthook(*sys.exc_info())
			return False

	def check_http_msg_signature(self, msg):
		u = msg.get_uri()
		return self.check_http_signature(u.get_path(), u.get_query())

	def player_websocket_cb(self, server, msg, path, conn):
		(upath, query) = path.split(":", 1)
		if self.check_http_signature(upath, query) is False:
			conn.close(403, "whatever")
			return

		cs = ClientSession(self, conn, self.next_connid)
		self.connections[self.next_connid] = cs
		self.next_connid = self.next_connid + 1

	def player_websocket_closed(self, connection, connid):
		self.connections.pop(connid)

	def dispatch(self, message):
		m = json.dumps(message)
		for c in self.connections.values():
			c.dispatch(m)

	def album_art_filename(self, filename):
		if filename is None:
			return None

		if filename.startswith(self.artcache) is False:
			return None

		rfn = filename[len(self.artcache):].lstrip('/')
		return os.path.normpath(rfn)


	def entry_details(self, entry):
		m = {}
		if entry is not None:
			for (p, k) in self.string_props.items():
				m[k] = entry.get_string(p)
			for (p, k) in self.ulong_props.items():
				m[k] = entry.get_ulong(p)

			key = entry.create_ext_db_key(RB.RhythmDBPropType.ALBUM)
			(filename, lkey) = self.art_store.lookup(key)
			m['albumart'] = self.album_art_filename(filename)
		return m

	def set_playing_position(self, update):
		try:
			(r, pos) = self.shell_player.get_playing_time()
			update['position'] = pos * 1000
		except Exception as e:
			pass

	def client_status(self, message):
		entry = self.shell_player.get_playing_entry()
		if entry:
			m = self.entry_details(entry)
			self.set_playing_position(m)
			p = self.shell_player.get_playing()
			m['playing'] = p[1]
		else:
			m = { 'playing': False, 'id': 0 }

		m['hostname'] = GLib.get_host_name()
		
		# Add volume state
		m['volume'] = self.shell_player.get_volume()[1]

		m.update(self.playmode_state())
		return m

	def playmode_state(self):
		(r, shuffle, repeat) = self.shell_player.get_playback_state()
		if not repeat:
			self.repeat_one = False
		if self.repeat_one:
			mode = 'one'
		elif repeat:
			mode = 'all'
		else:
			mode = 'off'
		return {'shuffle': bool(shuffle), 'repeat': mode}

	def client_playmode(self, message):
		try:
			(r, shuffle, repeat) = self.shell_player.get_playback_state()
			if 'shuffle' in message:
				shuffle = bool(message['shuffle'])
			mode = message.get('repeat')
			if mode in ('off', 'all', 'one'):
				# "one" keeps Rhythmbox's own repeat on, the closest built-in mode
				repeat = mode != 'off'
				self.repeat_one = mode == 'one'
			self.shell_player.set_playback_state(shuffle, repeat)
			state = self.playmode_state()
			self.dispatch(state)
			r = {'result': 'ok'}
			r.update(state)
			return r
		except Exception as e:
			return {'result': str(e)}

	def play_order_changed_cb(self, player, pspec):
		# shuffle/repeat changed, possibly from Rhythmbox's own window
		self.dispatch(self.playmode_state())

	def replay_entry(self, entry):
		try:
			source = self.shell_player.get_playing_source() or self.object.props.library_source
			self.shell_player.play_entry(entry, source)
		except Exception as e:
			sys.excepthook(*sys.exc_info())
		return False

	def client_next(self, message):
		try:
			self.shell_player.do_next()
			return {'result': 'ok'}
		except Exception as e:
			return {'result': str(e) }

	def client_previous(self, message):
		try:
			self.shell_player.do_previous()
			return {'result': 'ok'}
		except Exception as e:
			return {'result': str(e) }

	def client_playpause(self, message):
		try:
			self.shell_player.playpause()
			return {'result': 'ok'}
		except Exception as e:
			return {'result': str(e) }

	def client_seek(self, message):
		try:
			self.shell_player.set_playing_time(max(0, int(message['time'])))
			# Rhythmbox doesn't announce the new position when paused or for
			# small jumps, so report it to the client directly
			r = {'result': 'ok'}
			self.set_playing_position(r)
			return r
		except Exception as e:
			return {'result': str(e) }

	def client_volume(self, message):
		try:
			if 'volume' in message:
				# Set volume (0.0 to 1.0)
				volume = float(message['volume'])
				volume = max(0.0, min(1.0, volume))
				self.shell_player.set_volume(volume)
			# Always return current volume
			current_volume = self.shell_player.get_volume()[1]
			return {'result': 'ok', 'volume': current_volume}
		except Exception as e:
			return {'result': str(e)}



	search_limit_default = 50
	search_limit_max = 200
	search_props = (
		RB.RhythmDBPropType.TITLE,
		RB.RhythmDBPropType.ARTIST,
		RB.RhythmDBPropType.ALBUM,
	)

	@staticmethod
	def fold(text):
		# case- and accent-insensitive form used for matching and sorting
		text = unicodedata.normalize('NFKD', text or '')
		return ''.join(c for c in text if not unicodedata.combining(c)).casefold()

	# columns the client can sort by
	sort_columns = ('title', 'artist', 'album', 'year', 'duration')

	def invalidate_library_cache(self, *args):
		self.library_cache = None
		self.sorted_cache = {}

	def get_library(self):
		# list of (entry, folded search text, column sort values), sorted like
		# the library view: artist, album, disc, track, title.
		# rebuilt lazily after db changes.
		if self.library_cache is not None:
			return self.library_cache

		rows = []
		def collect(entry):
			if entry.get_boolean(RB.RhythmDBPropType.HIDDEN):
				return
			artist = self.fold(entry.get_string(RB.RhythmDBPropType.ALBUM_ARTIST) or
					   entry.get_string(RB.RhythmDBPropType.ARTIST))
			sortkey = (artist,
				   self.fold(entry.get_string(RB.RhythmDBPropType.ALBUM)),
				   entry.get_ulong(RB.RhythmDBPropType.DISC_NUMBER),
				   entry.get_ulong(RB.RhythmDBPropType.TRACK_NUMBER),
				   self.fold(entry.get_string(RB.RhythmDBPropType.TITLE)))
			text = '\n'.join(self.fold(entry.get_string(p)) for p in self.search_props)
			# None marks an unknown value; those always sort last
			values = {
				'title': self.fold(entry.get_string(RB.RhythmDBPropType.TITLE)) or None,
				'artist': self.fold(entry.get_string(RB.RhythmDBPropType.ARTIST)) or None,
				'album': self.fold(entry.get_string(RB.RhythmDBPropType.ALBUM)) or None,
				'year': entry.get_ulong(RB.RhythmDBPropType.YEAR) or None,
				'duration': entry.get_ulong(RB.RhythmDBPropType.DURATION) or None,
			}
			rows.append((sortkey, entry, text, values))

		self.db.entry_foreach_by_type(self.db.entry_type_get_by_name('song'), collect)
		rows.sort(key=lambda r: r[0])
		self.library_cache = [(entry, text, values) for (sortkey, entry, text, values) in rows]
		return self.library_cache

	def get_sorted_library(self, sort, descending):
		# the library sorted by one column; ties keep library order
		if sort not in self.sort_columns:
			return self.get_library()

		key = (sort, descending)
		if key not in self.sorted_cache:
			rows = self.get_library()
			known = [r for r in rows if r[2][sort] is not None]
			unknown = [r for r in rows if r[2][sort] is None]
			known.sort(key=lambda r: r[2][sort], reverse=descending)
			self.sorted_cache[key] = known + unknown
		return self.sorted_cache[key]

	def client_search(self, message):
		try:
			query = str(message.get('query') or '')
			offset = max(0, int(message.get('offset', 0)))
			limit = int(message.get('limit', self.search_limit_default))
			limit = max(1, min(self.search_limit_max, limit))

			sort = message.get('sort')
			descending = message.get('order') == 'desc'

			# every word must appear in the title, artist or album
			words = self.fold(query).split()
			matches = [entry for (entry, text, values) in self.get_sorted_library(sort, descending)
				   if all(w in text for w in words)]

			results = [self.entry_details(e) for e in matches[offset:offset + limit]]
			return {'search': {
				'seq': message.get('seq'),
				'query': query,
				'sort': sort if sort in self.sort_columns else None,
				'order': 'desc' if descending else 'asc',
				'offset': offset,
				'total': len(matches),
				'results': results
			}}
		except Exception as e:
			sys.excepthook(*sys.exc_info())
			return {'search': {'seq': message.get('seq'), 'error': str(e)}}

	def client_play(self, message):
		try:
			entry = self.db.entry_lookup_by_id(int(message['id']))
			if entry is None:
				return {'result': 'not found'}
			# play from the library so next/previous continue through it
			self.shell_player.play_entry(entry, self.object.props.library_source)
			return {'result': 'ok'}
		except Exception as e:
			return {'result': str(e)}

	def playing_song_changed_cb(self, player, entry):
		previous = self.current_entry
		if self.repeat_one and previous is not None and entry != previous:
			# replay only if the previous song played to its end; pressing
			# next/previous or picking another song moves on as usual
			duration = previous.get_ulong(RB.RhythmDBPropType.DURATION)
			if duration > 0 and self.elapsed >= (duration - 3) * 1000000000:
				GLib.idle_add(self.replay_entry, previous)

		self.current_entry = entry
		self.elapsed = 0
		self.dispatch(self.entry_details(entry))

	def playing_changed_cb(self, player, playing):
		u = { 'playing': playing }
		self.set_playing_position(u)
		self.dispatch(u)

	def playing_song_property_changed_cb(self, player, uri, prop, oldvalue, newvalue):
		if prop in self.string_props:
			self.dispatch({ self.string_props[prop]: newvalue })
		if prop in self.ulong_props:
			self.dispatch({ self.ulong_props[prop]: newvalue })

	def elapsed_nano_changed_cb(self, player, elapsed):
		if abs(elapsed - self.elapsed) > 1000000000:
			self.dispatch({'position': elapsed/1000000})	# ms
		self.elapsed = elapsed

	def art_added_cb(self, store, key, filename, data):
		entry = self.shell_player.get_playing_entry()
		if entry is not None and self.db.entry_matches_ext_db_key(entry, key):
			self.dispatch({'albumart': self.album_art_filename(filename)})

	def send_file_response(self, msg, filename, content_type):
		try:
			fp = open(filename, 'rb')
			d = fp.read()

			if callable(content_type):
				content_type = content_type(d)

			msg.set_response(content_type, Soup.MemoryUse.COPY, d)
			msg.set_status(200)
		except Exception as e:
			sys.excepthook(*sys.exc_info())
			msg.set_status(500)

	def image_content_type(self, data):
		# superhacky
		if data[1:4] == b'PNG':
			return "image/png"
		elif data[0:5] == b'<?xml':
			return "image/svg+xml"
		elif data[0:4] == b'<svg':
			return "image/svg+xml"
		else:
			return "image/jpeg"

	def http_track_cb(self, server, msg, path, query):

		if self.check_http_msg_signature(msg) is False:
			msg.set_status(403)
			return

		entry = self.shell_player.get_playing_entry()
		if entry is None:
			msg.set_status(404)
			return

		mt = entry.get_string(RB.RhythmDBPropType.MEDIA_TYPE)
		ct = RB.gst_media_type_to_mime_type(mt)
		s = TrackStreamer(server, msg, entry.get_playback_uri(), ct)
		try:
			s.open()
		except Exception as e:
			sys.excepthook(*sys.exc_info())
			msg.set_status(500)


	def http_art_cb(self, server, msg, path, query):
		if self.check_http_msg_signature(msg) is False:
			msg.set_status(403)
			return

		if msg.get_method() != "GET":
			msg.set_status(404)
			return

		if re.match("/art/[^/][a-zA-Z0-9/]+", path) is None:
			msg.set_status(404)
			return

		artpath = os.path.join(self.artcache, path[len("/art/"):])
		if not os.path.exists(artpath):
			msg.set_status(404)
			return

		self.send_file_response(msg, artpath, self.image_content_type)

	def http_icon_cb(self, server, msg, path, query):
		if msg.get_method() != "GET":
			msg.set_status(404)
			return

		if re.match("/icon/[a-zA-Z0-9.-]+/[0-9]+", path) is None:
			msg.set_status(404)
			return

		bits = path.split("/")
		iconname = bits[2]
		iconsize = int(bits[3])
		icon = Gtk.IconTheme.get_default().lookup_icon(iconname, iconsize, Gtk.IconLookupFlags.FORCE_SVG)
		if icon is None:
			msg.set_status(404)
			return

		iconfile = icon.get_filename()
		try:
			res = Gio.resources_lookup_data(iconfile, 0)
			data = res.get_data()
			content_type = self.image_content_type(data)
			msg.set_response(content_type, Soup.MemoryUse.COPY, data)
			msg.set_status(200)
		except gi.repository.GLib.GError as ge:
			# assume we couldn't find the resource, so try it as a filename
			self.send_file_response(msg, icon.get_filename(), self.image_content_type)
		except Exception as e:
			sys.excepthook(*sys.exc_info())
			msg.set_status(500)


	def serve_static(self, msg, path, subdir, content_type):

		if subdir == '':
			ssubdir = '/'
		else:
			ssubdir = "/" + subdir + "/"
		if not path.startswith(ssubdir):
			msg.set_status(403)
			return

		relpath = path[len(ssubdir):]

		if msg.get_method() != "GET" or relpath.find("/") != -1:
			msg.set_status(403)
			return

		f = rb.find_plugin_file(self, os.path.join(subdir, relpath))
		if f is None:
			msg.set_status(403)
			return

		self.send_file_response(msg, f, content_type)


	def http_static_css_cb(self, server, msg, path, query):
		self.serve_static(msg, path, "css", "text/css")

	def http_static_js_cb(self, server, msg, path, query):
		self.serve_static(msg, path, "js", "text/javascript")

	def http_root_cb(self, server, msg, path, query):
		self.serve_static(msg, '/webremote.html', '', 'text/html')

	def settings_changed_cb(self, settings, key):
		if key == 'listen-port':
			if self.http_server is not None and self.listen_reset is False:
				self.http_server.disconnect()
				self.http_listen()
		elif key == 'access-key':
			for c in self.connections.values():
				c.disconnect()

	def http_listen(self):
		print("relistening")
		port = self.settings['listen-port']
		if port != 0:
			try:
				self.http_server.listen_all(port, 0)
			except Exception as e:
				port = 0

		if port == 0:
			print("trying again")
			self.listen_reset = True
			self.http_server.listen_all(0, 0)
			# remember the port number for convenience
			uris = self.http_server.get_uris()
			if len(uris) > 0:
				self.settings['listen-port'] = uris[0].get_port()

			self.listen_reset = False


	def do_activate(self):
		shell = self.object
		self.db = shell.props.db

		self.artcache = os.path.join(RB.user_cache_dir(), "album-art", "")
		self.art_store = RB.ExtDB(name="album-art")
		self.art_store.connect("added", self.art_added_cb)

		self.db.connect("entry-added", self.invalidate_library_cache)
		self.db.connect("entry-deleted", self.invalidate_library_cache)
		self.db.connect("entry-changed", self.invalidate_library_cache)

		self.shell_player = shell.props.shell_player
		self.shell_player.connect("playing-song-changed", self.playing_song_changed_cb)
		self.shell_player.connect("playing-song-property-changed", self.playing_song_property_changed_cb)
		self.shell_player.connect("playing-changed", self.playing_changed_cb)
		self.shell_player.connect("elapsed-nano-changed", self.elapsed_nano_changed_cb)
		self.shell_player.connect("notify::play-order", self.play_order_changed_cb)
		self.playing_song_changed_cb(self.shell_player, self.shell_player.get_playing_entry())

		self.http_server = Soup.Server()
		self.http_server.add_handler(path="/art/", callback=self.http_art_cb)
		self.http_server.add_handler(path="/icon/", callback=self.http_icon_cb)
		self.http_server.add_handler(path="/entry/current/stream", callback=self.http_track_cb)
		self.http_server.add_handler(path="/css/", callback=self.http_static_css_cb)
		self.http_server.add_handler(path="/js/", callback=self.http_static_js_cb)
		self.http_server.add_websocket_handler("/ws/player", None, None, self.player_websocket_cb)
		self.http_server.add_handler(path="/", callback=self.http_root_cb)

		self.http_listen()


	def do_deactivate(self):
		self.dispatch({'shutdown': True })
		self.server = None
		self.connections = {}

class WebRemoteConfig(GObject.Object, PeasGtk.Configurable):
	__gtype_name__ = 'WebRemoteConfig'
	object = GObject.property(type=GObject.Object)

	def accesskey_focus_out_cb(self, widget, event):
		k = widget.get_text()
		if k != self.access_key:
			print("changing access key to %s" % k)
			self.access_key = k
			self.settings['access-key'] = k

		return False

	def update_port(self):
		hostname = get_host_name()
		port = self.settings['listen-port']
		url = 'http://%s:%d/' % (hostname, port)
		label = _('Launch web remote control')
		self.launch_link.set_markup('<a href="%s">%s</a>' % (url, label))

		self.portnumber.set_text("%d" % port)

	def settings_changed_cb(self, settings, key):
		if key == 'listen-port':
			self.update_port()

	def do_create_configure_widget(self):
		self.settings = Gio.Settings.new("org.gnome.rhythmbox.plugins.webremote")
		self.settings.connect("changed", self.settings_changed_cb)

		ui_file = rb.find_plugin_file(self, "webremote-config.ui")
		self.builder = Gtk.Builder()
		self.builder.add_from_file(ui_file)

		content = self.builder.get_object("webremote-config")

		self.portnumber = self.builder.get_object("portnumber")
		self.launch_link = self.builder.get_object("launch-link")
		self.update_port()

		self.key_entry = self.builder.get_object("accesskey")
		self.access_key = self.settings['access-key']
		if self.access_key:
			self.key_entry.set_text(self.access_key)
		self.key_entry.connect("focus-out-event", self.accesskey_focus_out_cb)

		return content


GObject.type_register(WebRemoteConfig)
