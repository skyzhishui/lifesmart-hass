"""LifeSmart LAN (offline) support @skyzhishui

Talks to the Smart Station directly over TCP 8888 (the App "local mode"
protocol, reverse engineered by @MagicBear, ref implementation by
MapleEve/lifesmart-for-homeassistant, MIT). Ported into this repo so the
integration has no dependency on that project.

Protocol notes verified against station firmware 1.2.3.2:
- frame  = "GL00\\x00\\x00" + 4B big-endian payload length + payload
           (payload >= 1000 bytes -> "ZZ00..." gzip, length = uncompressed)
- body   = TLV tree: 0x00 null, 0x02/0x03 bool, 0x04 zigzag varint,
           0x05 idx+FIXED 7 bytes, 0x06 idx+varint (timestamp),
           0x11 varint-prefixed utf8/bytes, 0x12 1B-count dict/list,
           0x13 1B enum key, 0x01 empty list
  (MapleEve reads 0x05 as idx+8 bytes which desyncs on current firmware;
   measured: the record is idx + 7 bytes)
- login  : act="Login", node="<uuid>/ac", args uid/pwd/cid/... (plaintext)
- devices: act="enum:91" (get-config), reply ret[1]["eps"]
- control: act="rfSetA", node="<agtid>/ep", args {val, valtag:"m",
           devid, key:<idx>, type:0x81/0x80/...} (same codes as cloud EpSet)
- scenes : act="RunA", node="<agtid>/ai", args {cron_name:"AI_IR_..."}
- pushes : decoded[1]["_schg"] = {<agt>/me/ep/<devid>/m/<idx>: {chg: {...io}}}

This module is Home-Assistant-free so it can be unit tested standalone.
"""
import gzip
import io
import logging
import socket
import struct
import threading
import time

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 8
HEARTBEAT_SECONDS = 60
RECONNECT_SECONDS = 10

# ---------------------------------------------------------------------------
# wire protocol
# ---------------------------------------------------------------------------

KEY_MAPPING = {
    2: "timestamp", 3: "req", 4: "args", 7: "valtag", 9: "act", 10: "node",
    11: "ret", 13: "cron_name", 16: "name", 21: "ts", 22: "devid",
    38: "nid", 39: "cls", 40: "rf_button_status", 41: "val", 42: "ver",
    46: "cgy", 47: "key", 53: "response_token", 78: "type", 89: "icon",
    90: "uuid", 92: "act_change", 106: "_chd", 113: "agtid", 119: "tmzone",
    120: "rf_pair", 121: "valtag", 122: "valts", 135: "dark", 136: "bright",
}
REVERSE_KEY_MAPPING = {v: k for k, v in KEY_MAPPING.items()}


class LSTimestamp:
    """Timestamp value inside a decoded packet."""

    def __init__(self, index, value):
        self.index = index
        self.value = value

    def __repr__(self):
        return f"<LSTimestamp {self.value}>"


def _encode_varint(value):
    """Encode an integer as a varint."""
    data = bytearray()
    while value >= 128:
        data.append((value & 0x7F) | 0x80)
        value >>= 7
    data.append(value)
    return bytes(data)


def _decode_varint(stream):
    """Decode a varint from the stream."""
    value, shift = 0, 0
    while True:
        byte = stream.read(1)
        if not byte:
            raise EOFError("incomplete varint")
        b = byte[0]
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value
        shift += 7


class LifeSmartProtocol:
    """Encoder/decoder for the station binary TLV protocol."""

    def _pack_value(self, value, is_key=False):
        """Pack a python value into TLV bytes."""
        if isinstance(value, bool):
            return b"\x02" if value else b"\x03"
        if isinstance(value, int):
            zz = (value << 1) ^ (value >> 31)
            return b"\x04" + _encode_varint(zz)
        if isinstance(value, str):
            if value.startswith("enum:"):
                key = value[5:]
                try:
                    return struct.pack("BB", 0x13, int(key))
                except ValueError:
                    pass
                enum_id = REVERSE_KEY_MAPPING.get(key)
                if enum_id is not None:
                    return struct.pack("BB", 0x13, enum_id)
            if is_key and REVERSE_KEY_MAPPING.get(value):
                return struct.pack("BB", 0x13, REVERSE_KEY_MAPPING[value])
            raw = value.encode("utf-8")
            return b"\x11" + _encode_varint(len(raw)) + raw
        if isinstance(value, bytes):
            return b"\x11" + _encode_varint(len(value)) + value
        if isinstance(value, dict):
            data = b"\x12" + struct.pack("B", len(value))
            for k, v in value.items():
                data += self._pack_value(k, True) + self._pack_value(v)
            return data
        if value is None:
            return b"\x00"
        raise TypeError(f"unsupported type: {type(value)}")

    def encode(self, parts):
        """Encode top-level dicts into one GL00 frame."""
        data = b""
        for part in parts:
            packed = self._pack_value(part)
            data += packed[1:]  # strip the leading 0x12 dict marker
        pkt = b"GL00\x00\x00" + struct.pack(">I", len(data)) + data
        if len(pkt) >= 1000:
            return b"ZZ00\x00\x00" + struct.pack(">I", len(pkt)) + gzip.compress(pkt)
        return pkt

    def _parse_value(self, stream, data_type):
        """Decode one TLV value."""
        if data_type == 0x00:
            return None
        if data_type == 0x01:
            return []
        if data_type == 0x02:
            return True
        if data_type == 0x03:
            return False
        if data_type == 0x04:
            zz = _decode_varint(stream)
            return (zz >> 1) ^ -(zz & 1)
        if data_type == 0x05:
            # idx + FIXED 7 bytes payload on firmware 1.2.3.2
            stream.read(1)
            return stream.read(7)
        if data_type == 0x06:
            stream.read(1)
            zz = _decode_varint(stream)
            return LSTimestamp(0, (zz >> 1) ^ -(zz & 1))
        if data_type == 0x11:
            raw = stream.read(_decode_varint(stream))
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw
        if data_type == 0x12:
            count = stream.read(1)[0]
            items = []
            for _ in range(count):
                key = self._parse_value(stream, stream.read(1)[0])
                value = self._parse_value(stream, stream.read(1)[0])
                items.append((key, value))
            keys = [k for k, _ in items]
            if count and all(isinstance(k, int) for k in keys) \
                    and keys == list(range(count)):
                return [v for _, v in items]
            return {k: v for k, v in items}
        if data_type == 0x13:
            enum_id = stream.read(1)[0]
            return f"enum:{KEY_MAPPING.get(enum_id, enum_id)}"
        raise ValueError(f"unknown data type 0x{data_type:02x}")

    def _normalize(self, data):
        """Strip enum: prefixes from keys, unwrap LSTimestamp values."""
        if isinstance(data, dict):
            out = {}
            for k, v in data.items():
                if isinstance(k, str) and k.startswith("enum:"):
                    k = k[5:]
                out[k] = self._normalize(v)
            return out
        if isinstance(data, list):
            return [self._normalize(v) for v in data]
        if isinstance(data, LSTimestamp):
            return data.value
        return data

    def decode_payload(self, payload):
        """Decode a frame payload into a list of top-level dicts."""
        stream = io.BytesIO(payload)
        result = []
        while stream.tell() < len(payload):
            result.append(self._normalize(self._parse_value(stream, 0x12)))
        return result

    def decode_frames(self, buf):
        """Decode all complete frames in buf; return (elements, remaining)."""
        elements = []
        while len(buf) >= 10:
            header, pkt_len = buf[:4], struct.unpack(">I", buf[6:10])[0]
            if len(buf) < 10 + pkt_len:
                break
            payload, buf = buf[10:10 + pkt_len], buf[10 + pkt_len:]
            if header == b"ZZ00":
                payload = gzip.decompress(payload)
            try:
                elements.extend(self.decode_payload(payload))
            except (ValueError, EOFError, OSError) as err:
                _LOGGER.warning("lifesmart local: bad frame skipped: %s", err)
        return elements, buf


class LifeSmartPacketFactory:
    """Build the control packets we need (login / get-config / epset / RunA)."""

    def __init__(self, node_agt="", node=""):
        self._proto = LifeSmartProtocol()
        self.node_agt = node_agt
        self.node = node

    def _build_packet(self, args, act="rfSetA", node_suffix="/ep"):
        """Build one standard control packet."""
        return self._proto.encode([
            {"_sel": 1, "req": False, "timestamp": 10},
            {"args": args, "node": f"{self.node_agt}{node_suffix}", "act": act},
        ])

    def build_login_packet(self, uid, pwd):
        """Build the login packet."""
        return self._proto.encode([
            {"_sel": 1, "sn": 1, "req": False},
            {
                "args": {
                    "cid": "6D56899B-82DA-403D-8291-50B57EE05DBA",
                    "cver": "1.0.48p1",
                    "uid": uid,
                    "nick": "admin",
                    "cname": "LifeSmart",
                    "pwd": pwd,
                },
                "node": "A3MAAABaAEkBRzQ0Mzc0OA/ac",
                "act": "Login",
            },
        ])

    def build_get_config_packet(self, node):
        """Build the get-config (enum:91) packet that lists all eps."""
        config_data = [
            {"req": False, "timestamp": 10},
            {
                "args": {
                    "enum:14": {
                        "enum:98": {
                            "uuid": False,
                            "enum:114": False,
                            "ver": False,
                            "enum:14": {"m": 1, "s": False, "_chd": 1},
                            "icon": False,
                            "cls": False,
                            "enum:56": False,
                            "_": "eps",
                            "P_Flip": False,
                            "ptzmr": False,
                            "enum:83": False,
                            "nid": False,
                            "devType": False,
                            "cgy": False,
                            "enum:108": False,
                            "rfic": False,
                            "name": False,
                            "agtid": False,
                        }
                    },
                    "enum:12": {"enum:13": False},
                    "_chd": 1,
                },
                "node": f"{node}/me/ep",
                "act": "enum:91",
            },
        ]
        return self._proto.encode(config_data)

    def build_epset_packet(self, devid, idx, command_type, val):
        """Build a single-io control packet (same codes as cloud EpSet)."""
        return self._build_packet({
            "val": val,
            "valtag": "m",
            "devid": devid,
            "key": idx,
            "type": command_type,
        })

    def build_run_scene_packet(self, scene_name):
        """Build a RunA packet that triggers a station scene by name."""
        return self._build_packet(
            {"cron_name": scene_name}, act="RunA", node_suffix="/ai"
        )


# ---------------------------------------------------------------------------
# device normalization: local eps -> cloud EpGetAll-shaped dicts
# ---------------------------------------------------------------------------

def _norm_devtype(cls):
    """Strip the firmware _V1/_V2/_V3 suffix so devtype matches cloud."""
    return cls[:-3] if len(cls) >= 3 and cls[-3:-1] == "_V" else cls


def _v_value(devtype, idx, val):
    """Recompute the cloud 'v' field from the raw val (None = unknown)."""
    if idx in ("T", "H", "tT"):
        return val / 10 if isinstance(val, (int, float)) else None
    if devtype == "SL_CP_DN" and idx in ("P3", "P4"):
        return val / 10 if isinstance(val, (int, float)) else None
    if idx == "V":
        # battery %: the firmware battery gauge is not in the local stream,
        # fit against cloud data (val 127->0, 144->25, 145->50): one 25% bar
        # per raw step above 143, clamped to [0, 100]
        if not isinstance(val, (int, float)):
            return None
        return max(0, min(100, 25 * (val - 143)))
    return val


def normalize_devices(eps, agt):
    """Turn the eps table into dicts shaped like cloud EpGetAll results."""
    devices = []
    for devid, dev in eps.items():
        if not isinstance(dev, dict) or "cls" not in dev:
            continue
        devtype = _norm_devtype(dev["cls"])
        m = (dev.get("_chd") or {}).get("m") or {}
        ios = m.get("_chd") if isinstance(m.get("_chd"), dict) else {}
        data = {}
        for idx, io in ios.items():
            if not isinstance(io, dict):
                continue
            io_type = io.get("type", 0)
            if io_type == 30:
                # K/I button-event channels exist only locally (type 30);
                # the cloud device data never carries them, so drop them to
                # keep entity sets identical across modes
                continue
            val = io.get("val")
            entry = {"type": io_type, "val": val}
            v = _v_value(devtype, idx, val)
            if v is not None:
                entry["v"] = v
            data[idx] = entry
        devices.append({
            "agt": agt,
            "me": devid,
            "devtype": devtype,
            "name": dev.get("name") or devid,
            "data": data,
        })
    return devices


# ---------------------------------------------------------------------------
# connection helpers
# ---------------------------------------------------------------------------

class LifeSmartLocalConnection:
    """One TCP session against the station (login + get-config + requests)."""

    def __init__(self, host, username, password):
        self.host = host
        self.username = username
        self.password = password
        self.sock = None
        self._buf = b""
        self._proto = LifeSmartProtocol()
        self._factory = LifeSmartPacketFactory()
        self.node = ""
        self.node_agt = ""

    def connect(self):
        """Connect and login; raises on failure."""
        self.sock = socket.create_connection((self.host, 8888), timeout=REQUEST_TIMEOUT)
        self.sock.settimeout(REQUEST_TIMEOUT)
        self.sock.sendall(self._factory.build_login_packet(
            self.username, self.password))
        parts = self._read_until(lambda p: any(
            isinstance(x, dict) and "ret" in x for x in p))
        login = next(x for x in parts if isinstance(x, dict) and "ret" in x)
        if login["ret"] is None:
            self.close()
            raise PermissionError(f"station login failed: {login}")
        # login reply keys decode to ints: ret[4] = {base: {1: agt}, agt: ...}
        ret = login["ret"]
        node_info = ret.get(4) or ret.get("4") if isinstance(ret, dict) else None
        if node_info is None:
            self.close()
            raise PermissionError(f"station login reply malformed: {login}")
        base = node_info.get("base") or {}
        agt = node_info.get("agt") or {}
        self.node = base.get(1) or base.get("1") or ""
        self.node_agt = agt.get(1) or agt.get("1") or ""
        self._factory.node = self.node
        self._factory.node_agt = self.node_agt
        return self.node

    def get_config(self):
        """Fetch the eps table; returns the raw eps dict."""
        self.send_get_config()
        parts = self._read_until(lambda p: _find_eps(p) is not None)
        return _find_eps(parts)

    def send_get_config(self):
        """Send a get-config request (also used as heartbeat)."""
        self.sock.sendall(self._factory.build_get_config_packet(self.node))

    def send_epset(self, devid, idx, command_type, val):
        """Fire one rfSetA control command (state confirmed via push)."""
        self.sock.sendall(self._factory.build_epset_packet(
            devid, idx, command_type, val))

    def send_run_scene(self, scene_name):
        """Fire one RunA scene trigger."""
        self.sock.sendall(self._factory.build_run_scene_packet(scene_name))

    def read_frames(self, timeout=1.0):
        """Read bytes and decode all complete frames; returns new elements."""
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return []
        except OSError:
            raise ConnectionError("socket closed")
        if not chunk:
            raise ConnectionError("socket closed by station")
        self._buf += chunk
        elements, self._buf = self._proto.decode_frames(self._buf)
        return elements

    def _read_until(self, predicate, timeout=REQUEST_TIMEOUT):
        """Read frames until predicate(elements) is truthy; returns elements."""
        end = time.time() + timeout
        while time.time() < end:
            elements = self.read_frames(timeout=1.0)
            if predicate(elements):
                return elements
        raise TimeoutError("station reply timed out")

    def close(self):
        """Close the socket."""
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def _find_eps(parts):
    """Locate ret[1]['eps'] inside decoded parts."""
    for part in parts:
        ret = part.get("ret") if isinstance(part, dict) else None
        if isinstance(ret, dict) and 1 in ret and isinstance(ret[1], dict) \
                and "eps" in ret[1]:
            return ret[1]["eps"]
    return None


def probe(host, username, password, timeout=REQUEST_TIMEOUT):
    """One-shot login + device fetch, used by the config flow."""
    conn = LifeSmartLocalConnection(host, username, password)
    try:
        conn.connect()
        eps = conn.get_config()
        return conn.node, conn.node_agt, eps
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# background manager thread
# ---------------------------------------------------------------------------

class LifeSmartLocalManager(threading.Thread):
    """Keep a station connection alive: pushes, heartbeat, reconnect, epset.

    Callbacks (invoked in the thread context, wrapper must schedule them
    onto the event loop):
      on_devices(list)        -- normalized cloud-shaped device list
      on_push(dict)           -- {"type": "io", "msg": {...}} like cloud ws
      on_state(bool)          -- connection up/down transitions
    """

    def __init__(self, host, username, password, on_devices=None,
                 on_push=None, on_state=None):
        super().__init__(name="lifesmart-local", daemon=True)
        self.host = host
        self.username = username
        self.password = password
        self.on_devices = on_devices
        self.on_push = on_push
        self.on_state = on_state
        self.node = ""
        self.node_agt = ""
        self._run = threading.Event()
        self._send_lock = threading.Lock()
        self._conn = None
        self._devices = {}
        self._state = None

    # -- lifecycle ---------------------------------------------------------

    def run(self):
        """Connect and reconnect forever until stopped."""
        backoff = 0
        while self._run.is_set():
            try:
                conn = LifeSmartLocalConnection(
                    self.host, self.username, self.password)
                node = conn.connect()
                self.node, self.node_agt = node, conn.node_agt
                self._conn = conn
                backoff = 0
                self._set_state(True)
                _LOGGER.info("lifesmart local: connected to %s (%s)",
                             self.host, self.node)
                eps = conn.get_config()
                self._devices = eps
                self._emit_devices()
                self._serve(conn)
            except Exception as err:  # noqa: BLE001 - thread must survive
                _LOGGER.error("lifesmart local: connection error: %s", err)
            finally:
                self._set_state(False)
                if self._conn:
                    self._conn.close()
                    self._conn = None
            if not self._run.is_set():
                break
            self._run.wait(RECONNECT_SECONDS + backoff)
            backoff = min(backoff + 10, 60)

    def _serve(self, conn):
        """Read loop: dispatch pushes, heartbeat get-config, resync states."""
        last_heartbeat = time.time()
        pending_config = False
        while self._run.is_set():
            elements = conn.read_frames(timeout=1.0)
            for part in elements:
                self._handle_part(part)
            if pending_config:
                found = _find_eps(elements)
                if found is not None:
                    pending_config = False
                    self._diff_resync(found)
                    self._devices = found
            if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = time.time()
                try:
                    with self._send_lock:
                        conn.send_get_config()
                    pending_config = True
                except OSError:
                    raise ConnectionError("heartbeat send failed")

    def start_keep_alive(self):
        """Start keep alive mechanism."""
        self._run.set()
        self.start()

    def stop_keep_alive(self):
        """Stop the thread and close the connection."""
        self._run.clear()
        conn = self._conn
        if conn:
            conn.close()
        self.join(timeout=15)

    # -- state / callbacks ---------------------------------------------------

    def is_connected(self):
        """True while the station session is up."""
        return self._conn is not None and self._run.is_set()

    def _set_state(self, up):
        if self._state is up:
            return
        self._state = up
        if self.on_state:
            self.on_state(up)

    def _emit_devices(self):
        if self.on_devices:
            self.on_devices(normalize_devices(self._devices, self.node))

    def _handle_part(self, part):
        """Dispatch one decoded packet (schg pushes and rfSetA replies)."""
        if not isinstance(part, dict):
            return
        schg = part.get("_schg")
        if not isinstance(schg, dict):
            return
        for path, data in schg.items():
            if not isinstance(path, str):
                continue
            parts = path.split("/")
            devid = sub = None
            if len(parts) == 6 and parts[1] == "me" and parts[2] == "ep" \
                    and parts[4] == "m":
                devid, sub = parts[3], parts[5]
            elif len(parts) == 5 and parts[1] == "ep" and parts[3] == "m":
                devid, sub = parts[2], parts[4]
            if not devid or not sub:
                continue
            chg = data.get("chg") if isinstance(data, dict) else None
            if not isinstance(chg, dict):
                continue
            dev = self._devices.get(devid)
            devtype = _norm_devtype(dev["cls"]) if isinstance(dev, dict) \
                and "cls" in dev else ""
            io = dict(chg)
            io.setdefault("type", 0)
            io.setdefault("val", 0)
            v = _v_value(devtype, sub, io["val"])
            if v is not None:
                io["v"] = v
            if self.on_push:
                self.on_push({
                    "type": "io",
                    "msg": {
                        "agt": self.node,
                        "me": devid,
                        "idx": sub,
                        "devtype": devtype,
                        **io,
                    },
                })

    def _diff_resync(self, eps):
        """Emit pushes for channels whose type/val changed since last sync."""
        for devid, dev in eps.items():
            if not isinstance(dev, dict):
                continue
            old = self._devices.get(devid)
            ios = (((dev.get("_chd") or {}).get("m") or {}).get("_chd")) or {}
            for idx, io in ios.items():
                if not isinstance(io, dict):
                    continue
                prev = None
                if isinstance(old, dict):
                    prev = ((((old.get("_chd") or {}).get("m") or {})
                             .get("_chd")) or {}).get(idx)
                if isinstance(prev, dict) and \
                        prev.get("val") == io.get("val") and \
                        prev.get("type") == io.get("type"):
                    continue
                self._handle_part({"_schg": {
                    f"{self.node}/me/ep/{devid}/m/{idx}": {"chg": io}}})

    # -- control ---------------------------------------------------------

    def epset(self, agt, me, idx, ep_type, val):
        """Send one control command; 0 on success, -1 on failure."""
        conn = self._conn
        if not conn:
            return -1
        try:
            with self._send_lock:
                conn.send_epset(me, idx, int(ep_type, 16) if isinstance(
                    ep_type, str) else ep_type, val)
            return 0
        except (OSError, ValueError) as err:
            _LOGGER.error("lifesmart local: epset failed: %s", err)
            return -1

    def run_scene(self, scene_name):
        """Trigger a station scene (AI_IR_...) by name; 0/-1."""
        conn = self._conn
        if not conn:
            return -1
        try:
            with self._send_lock:
                conn.send_run_scene(scene_name)
            return 0
        except OSError as err:
            _LOGGER.error("lifesmart local: run_scene failed: %s", err)
            return -1
