import datetime
import os
import socket
import struct
import threading
import sys

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from Crypto.Cipher import Blowfish


# ============================================================
# PATHS AND CONFIGURATION
# ============================================================

# Determine the directory containing the application.
#
# When running as a normal Python script, __file__ points to
# the script itself.
#
# When running as a frozen executable (for example, packaged
# with PyInstaller), sys.executable points to the executable.
#
# This allows the same code to work in both development and
# packaged environments.
HERE = os.path.dirname(
    os.path.abspath(
        sys.executable if getattr(sys, "frozen", False)
        else __file__
    )
)


def find_game_dir():
    """
    Locate the game installation directory.

    The server needs to find yo3.idx because it uses the game
    directory to serve the game's patch/resource files over HTTP.

    Several possible locations are checked to support different
    directory layouts and development environments.

    Returns:
        str: Absolute path to the game directory.

    Raises:
        FileNotFoundError: If yo3.idx cannot be found.
    """

    # Possible locations of the game installation.
    # Paths are relative to the directory containing this script
    # or executable.
    candidates = (
        HERE,
        os.path.abspath(os.path.join(HERE, "..")),
        os.path.join(HERE, "test-game"),
        os.path.abspath(os.path.join(HERE, "..", "research", "test-game")),
        os.path.abspath(os.path.join(HERE, "..", "test-game")),
    )

    # Keep track of directories already checked.
    # This prevents duplicate checks if multiple candidate paths
    # resolve to the same location.
    checked = []

    for candidate in candidates:
        candidate = os.path.normpath(candidate)

        if candidate not in checked:
            checked.append(candidate)

        # yo3.idx is used as the marker indicating that this
        # directory contains the expected game installation.
        if os.path.isfile(os.path.join(candidate, "yo3.idx")):
            return candidate

    # None of the candidate directories contained yo3.idx.
    # Include the checked paths in the exception to make
    # installation/path problems easier to diagnose.
    raise FileNotFoundError(
        "Could not find yo3.idx. Checked: " + repr(checked)
    )


# Resolve the game directory once during startup.
GAME_DIR = find_game_dir()

# All diagnostic output is written to this file as well as
# being displayed in the console.
LOG = os.path.join(HERE, "gate_test.log")


# Network configuration.
#
# The server binds to localhost only, meaning it accepts
# connections from the local machine rather than exposing
# these test services to other computers on the network.
HOST = "127.0.0.1"

# TCP port used by the initial gate/login server.
GATE_PORT = 5730

# TCP port used by the main frontend/account server.
FRONT_PORT = 1213

# HTTP port used to serve game files.
HTTP_PORT = 80


# Protocol encryption configuration.
#
# KEY is the eight-byte Blowfish encryption key.
# Blowfish uses an eight-byte block size.
#
# MASK is XORed with the packet length when transmitting
# and receiving the packet header.
KEY = b"12345678"
MASK = 0x77777777


# The response sent to the client during its initial
# frontend protocol/version validation exchange.
REQ_VERSION_VALID = 0x219F


# Test account information.
#
# These values are placeholders for the locally hosted
# test account and are reused throughout the server.
ACCOUNT_USER_ID = 1001
ACCOUNT_NAME = "test"


# ============================================================
# PROFILE DATA
# ============================================================

# Account information returned in profile-related responses.
#
# The dictionary is converted into protocol fields by
# make_reply() and encode_value().
PROFILE = {
    "regulation_id": 1,
    "user_id": ACCOUNT_USER_ID,
    "uname": ACCOUNT_NAME,
    "dpoint": 0,
}

# Player information.
#
# These values describe the player entry returned when the
# client requests the available player list.
PLAYER_NAME = ACCOUNT_NAME
PLAYER_INDEX = 0
PLAYER_ID = ACCOUNT_USER_ID
PLAYER_DLV = 1

# Prebuilt empty item structure.
#
# This is sent as a list entry in the login/profile response.
# Its exact internal meaning depends on the game's protocol.
EMPTY_ITEM = b"\x03\x00\x00\x00\x00"

# Multiple client connections may generate log messages
# simultaneously. A lock prevents their output from
# interfering with each other.
log_lock = threading.Lock()


# ============================================================
# LOGGING
# ============================================================

def log(message):
    """
    Write a timestamped message to the console and log file.

    The lock ensures that multiple server threads cannot
    write overlapping log entries.

    Args:
        message: Message to display and save.
    """

    # Generate a timestamp with millisecond precision.
    line = (
        f"{datetime.datetime.now():%H:%M:%S.%f}"[:-3]
        + "  "
        + str(message)
    )

    with log_lock:
        # Print immediately rather than waiting for the
        # console output buffer to flush.
        print(line, flush=True)

        # Append the same message to the persistent log file.
        # UTF-8 allows the log to contain non-ASCII characters.
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ============================================================
# NETWORK CODEC
# ============================================================

def swap_words(data):
    """
    Reverse the byte order of every four-byte word.

    The protocol applies this transformation before and after
    Blowfish encryption/decryption.

    Example:
        Input:  01 02 03 04 05 06 07 08
        Output: 04 03 02 01 08 07 06 05

    The input must contain a whole number of four-byte words.

    Args:
        data: Raw byte sequence.

    Returns:
        bytes: Byte-swapped data.
    """

    if len(data) % 4:
        raise ValueError(
            "Word swapping requires a multiple of four bytes"
        )

    return b"".join(
        data[i:i + 4][::-1]
        for i in range(0, len(data), 4)
    )


def encrypt(plain):
    """
    Encrypt a plaintext packet using Blowfish ECB.

    Encryption process:

        1. Pad the plaintext with null bytes until its length
           is divisible by Blowfish's eight-byte block size.
        2. Reverse the byte order of each four-byte word.
        3. Encrypt the result using Blowfish in ECB mode.
        4. Reverse the byte order of each encrypted four-byte word.

    Note that ECB mode does not provide authentication or
    integrity protection. This implementation is intended
    for compatibility with the existing game protocol.

    Args:
        plain: Unencrypted packet bytes.

    Returns:
        bytes: Encrypted packet body.
    """

    # Blowfish operates on eight-byte blocks.
    # Pad with null bytes when the plaintext is not aligned.
    plain += b"\0" * (-len(plain) % 8)

    # Create a new cipher instance for this operation.
    cipher = Blowfish.new(KEY, Blowfish.MODE_ECB)

    # Apply the protocol-specific word swapping around
    # the standard Blowfish encryption operation.
    return swap_words(
        cipher.encrypt(swap_words(plain))
    )


def decrypt(body):
    """
    Decrypt an encrypted packet body.

    This reverses the operations performed by encrypt():

        1. Check that the encrypted data is block-aligned.
        2. Reverse the byte order of each four-byte word.
        3. Decrypt using Blowfish ECB.
        4. Reverse the byte order again.

    The resulting plaintext may contain null padding bytes.

    Args:
        body: Encrypted packet bytes.

    Returns:
        bytes: Decrypted packet contents.
    """

    # Encrypted Blowfish data must always consist of complete
    # eight-byte blocks.
    if len(body) % 8:
        raise ValueError(
            "Encrypted body length is not a multiple of 8"
        )

    cipher = Blowfish.new(KEY, Blowfish.MODE_ECB)

    return swap_words(
        cipher.decrypt(swap_words(body))
    )


def frame(plain):
    """
    Construct a complete network packet.

    The packet consists of:

        [4-byte encoded length][encrypted packet body]

    The length field is stored as a big-endian unsigned
    32-bit integer and XORed with MASK.

    The length describes the encrypted body, not the
    original plaintext.

    Args:
        plain: Unencrypted protocol packet.

    Returns:
        bytes: Complete framed packet ready for transmission.
    """

    body = encrypt(plain)

    return (
        struct.pack(">I", len(body) ^ MASK)
        + body
    )


# ============================================================
# VALUE ENCODERS
# ============================================================

def n_val(tag, number):
    """
    Encode an integer-like protocol value.

    Supported tags:
        5: Numeric value.
        6: Alternative numeric value.

    Both formats are followed by a four-byte big-endian
    unsigned integer.

    Values are masked to 32 bits so that negative or oversized
    Python integers are represented within the protocol's
    unsigned 32-bit range.

    Args:
        tag: Protocol value type.
        number: Integer to encode.

    Returns:
        bytes: Encoded numeric value.
    """

    if tag not in (5, 6):
        raise ValueError("Numeric tag must be 5 or 6")

    return (
        bytes([tag])
        + struct.pack(">I", number & 0xFFFFFFFF)
    )


def t_val(tag, value):
    """
    Encode a length-prefixed text or byte-string value.

    Supported tags:
        7: Text.
        8: Alternative text representation.
        9: Raw byte data.

    The encoded structure is:

        [1-byte tag][4-byte length][data]

    Strings are converted to ASCII bytes. Existing byte
    sequences are preserved without modification.

    Args:
        tag: Protocol value type.
        value: String or bytes to encode.

    Returns:
        bytes: Encoded string/byte value.
    """

    if tag not in (7, 8, 9):
        raise ValueError("Text tag must be 7, 8 or 9")

    if isinstance(value, str):
        data = value.encode("ascii")
    else:
        data = bytes(value)

    return (
        bytes([tag])
        + struct.pack(">I", len(data))
        + data
    )


def encode_value(value):
    """
    Automatically select the appropriate encoder for a value.

    Supported Python types:

        bool  -> Numeric tag 5.
        int   -> Numeric tag 5.
        str   -> Text tag 7.
        bytes -> Byte-string tag 9.

    Booleans are checked before integers because Python's
    bool type is a subclass of int.

    Unsupported types raise TypeError rather than silently
    producing an invalid protocol value.

    Args:
        value: Python value to encode.

    Returns:
        bytes: Encoded protocol value.
    """

    if isinstance(value, bool):
        return n_val(5, int(value))

    if isinstance(value, int):
        return n_val(5, value)

    if isinstance(value, str):
        return t_val(7, value)

    if isinstance(value, bytes):
        return t_val(9, value)

    raise TypeError(
        f"Unsupported profile value type: {type(value).__name__}"
    )


def v_map(entries, tag=4, items=()):
    """
    Construct a protocol map/container.

    General structure:

        [1-byte container tag]
        [2-byte field count]

        Repeated fields:
            [1-byte field-name length]
            [field-name bytes]
            [encoded field value]

        [2-byte list-item count]
        [encoded list items]

    Field names are ASCII strings.

    The container supports both named fields and a separate
    list of nested values.

    Args:
        entries: Iterable of (field_name, encoded_value) pairs.
        tag: Container type identifier.
        items: Optional list of already-encoded values.

    Returns:
        bytes: Complete encoded container.
    """

    out = bytearray()

    # Container type followed by the number of named fields.
    out.append(tag)
    out.extend(struct.pack(">H", len(entries)))

    # Serialize each named field.
    for name, value in entries:
        name_bytes = name.encode("ascii")

        # The protocol stores the name length in one byte.
        if len(name_bytes) > 255:
            raise ValueError("Field name too long")

        out.append(len(name_bytes))
        out.extend(name_bytes)
        out.extend(value)

    # Convert the supplied iterable to a list so that its
    # length can be calculated and its contents serialized.
    items = list(items)

    # Append the number of nested list entries.
    out.extend(struct.pack(">H", len(items)))

    # Append each already-encoded list entry.
    for item in items:
        out.extend(item)

    return bytes(out)


def make_reply(request_id, fields=None, list_items=None):
    """
    Build a standard server response.

    Every response begins with a numeric 'request' field
    identifying the response type.

    Additional named fields and optional list entries are
    appended to the same top-level map.

    Args:
        request_id: Response identifier expected by the client.
        fields: Optional dictionary of additional fields.
        list_items: Optional list of encoded nested entries.

    Returns:
        bytes: Encoded response packet (not yet encrypted).
    """

    # The request identifier is always included first.
    entries = [
        ("request", n_val(5, request_id))
    ]

    # Convert all supplied Python values into their protocol
    # representations.
    for name, value in (fields or {}).items():
        entries.append((name, encode_value(value)))

    # Construct the final top-level map.
    return v_map(
        entries,
        tag=4,
        items=list_items or []
    )


# ============================================================
# PLAYER LIST
# ============================================================

def player_item(name, pindex, player_id, dlv):
    """
    Construct one player entry for the player-list response.

    Each player is represented as a nested protocol map
    using container tag 3.

    The initial fields contain the player's basic identity:

        pname     - Player name.
        pindex    - Position/index in the player list.
        player_id - Unique player identifier.
        dlv       - Player level/value.

    Additional fields were included after inspecting the
    client's hover/selection handling code.

    The client reads these fields from its stored player data.
    Leaving them out caused a CRT invalid-parameter failure.

    Their exact semantic types have not been independently
    confirmed, so neutral placeholder values are used.

    Args:
        name: Player display name.
        pindex: Player list index.
        player_id: Player identifier.
        dlv: Player level.

    Returns:
        bytes: Encoded player map.
    """

    fields = [
        ("pname", t_val(7, name)),
        ("pindex", n_val(5, pindex)),
        ("player_id", n_val(5, player_id)),
        ("dlv", n_val(5, dlv)),

        # Additional fields required by the client's
        # hover/select code at address 0x46C1E4.
        #
        # These values are placeholders. Their presence and
        # encoded types are based on observed client behaviour,
        # rather than a confirmed protocol specification.
        ("title", n_val(5, 0)),
        ("n_win", n_val(5, 0)),
        ("n_lose", n_val(5, 0)),
        ("team_level", n_val(5, 0)),
        ("team_name", t_val(7, "")),
        ("team_authority", n_val(5, 0)),
    ]

    # Player entries use tag 3 rather than the top-level
    # response map's tag 4.
    return v_map(fields, tag=3)


def make_player_list():
    """
    Generate the player list returned to the client.

    Currently, the test server exposes exactly one player.

    Returns:
        list[bytes]: Encoded player entries.
    """

    return [
        player_item(
            PLAYER_NAME,
            PLAYER_INDEX,
            PLAYER_ID,
            PLAYER_DLV
        )
    ]


# ============================================================
# PACKET PARSER
# ============================================================

def read_u16(data, pos):
    """
    Read a two-byte unsigned big-endian integer.

    Args:
        data: Packet being parsed.
        pos: Current byte offset.

    Returns:
        tuple: (integer value, updated offset).

    Raises:
        ValueError: If insufficient bytes remain.
    """

    if pos + 2 > len(data):
        raise ValueError("Truncated u16")

    return struct.unpack(">H", data[pos:pos + 2])[0], pos + 2


def read_u32(data, pos):
    """
    Read a four-byte unsigned big-endian integer.

    Used for numeric values and length-prefixed strings.

    Returns:
        tuple: (integer value, updated offset).
    """

    if pos + 4 > len(data):
        raise ValueError("Truncated u32")

    return struct.unpack(">I", data[pos:pos + 4])[0], pos + 4


def read_nested(data, pos):
    """
    Read a nested value from a map field.

    Unlike read_value(), this function handles the simpler
    value representation used inside named map fields.

    Supported nested types:

        Tags 5 and 6:
            Four-byte unsigned integer.

        Tags 7, 8 and 9:
            Four-byte length followed by raw data.

    Returns:
        tuple: (decoded value, updated offset).
    """

    if pos >= len(data):
        raise ValueError("Missing nested tag")

    # Every nested value begins with its type identifier.
    tag = data[pos]
    pos += 1

    if tag in (5, 6):
        return read_u32(data, pos)

    if tag in (7, 8, 9):
        length, pos = read_u32(data, pos)

        # Ensure the declared length does not extend beyond
        # the available packet data.
        if length > len(data) - pos:
            raise ValueError("Nested value exceeds packet")

        return data[pos:pos + length], pos + length

    raise ValueError(f"Unsupported nested tag {tag}")


def read_value(data, pos):
    """
    Recursively decode a protocol value.

    Supported structures:

        Tag 0:
            Length-prefixed byte string.

        Tag 1:
            Array/list of recursively encoded values.

        Tags 3-10:
            Map/container with named fields and list entries.

    Container structure:

        [tag]
        [2-byte field count]

        Repeated:
            [1-byte name length]
            [name]
            [nested value]

        [2-byte list count]
        [list entries]

    The parser performs bounds checks before reading data
    to prevent malformed packets from accessing invalid
    offsets.

    Args:
        data: Raw decrypted packet.
        pos: Starting byte offset.

    Returns:
        tuple: (decoded Python value, updated offset).
    """

    if pos >= len(data):
        raise ValueError("Missing value tag")

    tag = data[pos]
    pos += 1

    # --------------------------------------------------------
    # Tag 0: Length-prefixed byte string
    # --------------------------------------------------------
    if tag == 0:
        if pos >= len(data):
            raise ValueError("Missing string length")

        # This format uses a single-byte length.
        length = data[pos]
        pos += 1

        if length > len(data) - pos:
            raise ValueError("Truncated string")

        return data[pos:pos + length], pos + length

    # --------------------------------------------------------
    # Tag 1: Array/list
    # --------------------------------------------------------
    if tag == 1:
        # Number of elements in the array.
        count, pos = read_u16(data, pos)

        values = []

        # Each array element is itself a complete value and
        # therefore needs to be decoded recursively.
        for _ in range(count):
            value, pos = read_value(data, pos)
            values.append(value)

        return values, pos

    # --------------------------------------------------------
    # Tags 3-10: Map/container
    # --------------------------------------------------------
    if 3 <= tag <= 10:
        # Read the number of named fields.
        count, pos = read_u16(data, pos)

        # Sanity limit to reject implausibly large field counts.
        if count > 256:
            raise ValueError("Implausible field count")

        fields = {}

        # Decode every named field.
        for _ in range(count):
            if pos >= len(data):
                raise ValueError("Missing field name length")

            # Field names use a one-byte length prefix.
            name_length = data[pos]
            pos += 1

            if name_length > len(data) - pos:
                raise ValueError("Truncated field name")

            # Latin-1 allows every possible byte value to be
            # represented without decoding errors.
            name = data[pos:pos + name_length].decode("latin1")
            pos += name_length

            # Decode the value associated with this field.
            value, pos = read_nested(data, pos)
            fields[name] = value

        # Maps can also contain a separate array of nested
        # values after their named fields.
        list_count, pos = read_u16(data, pos)

        if list_count > 256:
            raise ValueError("Implausible list count")

        items = []

        for _ in range(list_count):
            value, pos = read_value(data, pos)
            items.append(value)

        # Return a structured Python representation of the map.
        return {
            "tag": tag,
            "fields": fields,
            "items": items
        }, pos

    # Any tag not handled above is considered unsupported.
    raise ValueError(f"Unknown value tag {tag}")


def parse_request(plain):
    """
    Parse a complete decrypted client request.

    The expected top-level structure is a map using tag 4.

    Returns:
        tuple:
            fields - Dictionary of named request fields.
            items  - List of nested request entries.

    Raises:
        ValueError: If the packet does not match the expected
                    top-level structure.
    """

    # Start parsing at the beginning of the packet.
    value, end = read_value(plain, 0)

    # The root value must be a map/container.
    if not isinstance(value, dict):
        raise ValueError("Top-level value is not a map")

    # The game expects incoming requests to use container tag 4.
    if value.get("tag") != 4:
        raise ValueError("Top-level map is not type 4")

    # Ensure the parser did not report an invalid ending offset.
    if end > len(plain):
        raise ValueError("Invalid packet boundary")

    return value["fields"], value["items"]


def describe(plain):
    """
    Produce a readable description of a decrypted packet.

    This is primarily a debugging function used to inspect
    incoming client traffic.

    When parsing succeeds, it displays:
        - Named fields.
        - Nested list entries.
        - Raw hexadecimal packet bytes.

    When parsing fails, the raw bytes and error are still
    displayed so that unknown packet formats can be studied.
    """

    try:
        fields, items = parse_request(plain)

        return (
            f"fields={fields!r}\n"
            f"list_items={items!r}\n"
            f"raw={plain.hex(' ')}"
        )

    except Exception as exc:
        return (
            f"raw={plain.hex(' ')}\n"
            f"parse error: {exc}"
        )


# ============================================================
# FRONT SERVER REQUESTS
# ============================================================

def handle_front_request(conn, count, plain):
    """
    Process a decrypted request received by the frontend server.

    The request is parsed, its numeric request identifier is
    extracted, and an appropriate response is generated.

    The currently implemented request handlers cover:

        0x1F47 - Authentication.
        0x1F48 - Login/profile.
        0x1F49 - Player list.
        0x1F4C - Player selection.

    Unknown requests are logged for further investigation.

    Args:
        conn: Active client socket.
        count: Number of packets received on this connection.
        plain: Decrypted request packet.
    """

    # Convert the raw packet into named fields and list entries.
    try:
        fields, list_items = parse_request(plain)

    except Exception as exc:
        log(f"[front] cannot parse packet: {exc}")
        return

    # Every supported frontend request is expected to contain
    # a numeric request identifier.
    request_id = fields.get("request")

    if not isinstance(request_id, int):
        log("[front] Missing numeric request field")
        return

    # Separate the request identifier from the remaining fields.
    # This makes the diagnostic output easier to read.
    other_fields = {
        key: value
        for key, value in fields.items()
        if key != "request"
    }

    log(
        f"[front] request 0x{request_id:X}; "
        f"fields={other_fields!r}; "
        f"list={list_items!r}"
    )

    # --------------------------------------------------------
    # Authentication: 0x1F47
    # --------------------------------------------------------
    #
    # The client requests account authentication.
    # Respond with the test account profile using response
    # identifier 0x219A.
    if request_id == 0x1F47:
        payload = make_reply(
            0x219A,
            PROFILE
        )

        # frame() encrypts the payload and adds the packet header.
        conn.sendall(frame(payload))
        log("[front] sent 0x219A account profile")

    # --------------------------------------------------------
    # Login/profile: 0x1F48
    # --------------------------------------------------------
    #
    # Return the account profile along with an empty item entry.
    # The additional list entry is part of the observed response
    # structure expected by the client.
    elif request_id == 0x1F48:
        payload = make_reply(
            0x2201,
            PROFILE,
            [EMPTY_ITEM]
        )

        conn.sendall(frame(payload))
        log("[front] sent 0x2201 account profile")

    # --------------------------------------------------------
    # Player list: 0x1F49
    # --------------------------------------------------------
    #
    # The client requests the list of available players.
    # Return a single test player using response identifier
    # 0x2260.
    elif request_id == 0x1F49:
        payload = make_reply(
            0x2260,
            {},
            make_player_list()
        )

        conn.sendall(frame(payload))

        log(
            "[front] sent 0x2260 player list; "
            f"player_id={PLAYER_ID}"
        )

        # Log the exact fields included in the player entry.
        # This is useful when comparing client behaviour
        # between different player-data structure versions.
        log(
            f"[front] 0x2260 payload {len(payload)} bytes; "
            "player item fields: pname, pindex, player_id, dlv, "
            "title, n_win, n_lose, team_level, team_name, team_authority "
            "(BUILD 2: extended player item)"
        )

    # --------------------------------------------------------
    # Player selection: 0x1F4C
    # --------------------------------------------------------
    #
    # The client sends the identifier of the player it wants
    # to select.
    elif request_id == 0x1F4C:
        selected_id = fields.get("player_id")

        # Reject requests without a valid numeric player ID.
        if not isinstance(selected_id, int):
            log(f"[front] invalid player_id: {selected_id!r}")
            return

        # This test server only has one configured player.
        if selected_id != PLAYER_ID:
            log(f"[front] unknown player_id {selected_id}")
            return

        # The requested player exists.
        # Return the account profile using response 0x2266.
        payload = make_reply(
            0x2266,
            PROFILE
        )

        conn.sendall(frame(payload))

        log(
            "[front] sent 0x2266 account profile; "
            f"player_id={selected_id}"
        )

    # --------------------------------------------------------
    # Unknown request
    # --------------------------------------------------------
    #
    # Instead of silently ignoring unrecognised requests,
    # record their identifiers, fields, list entries and raw
    # bytes for reverse-engineering and future implementation.
    else:
        log(
            f"[front] UNKNOWN request 0x{request_id:X}; "
            f"fields={other_fields!r}; "
            f"list={list_items!r}; "
            f"raw={plain.hex(' ')}"
        )


# ============================================================
# GATE RESPONSE
# ============================================================

def gate_reply():
    """
    Construct the initial gate-server response.

    The gate tells the client which frontend server to contact
    after completing the initial connection stage.

    Response fields:

        request - Gate response identifier (0x21FE).
        host    - Frontend server IP address.
        port    - Frontend server TCP port.

    Returns:
        bytes: Encoded gate response (not yet encrypted).
    """

    return v_map([
        ("request", n_val(5, 0x21FE)),
        ("host", t_val(7, HOST)),
        ("port", n_val(5, FRONT_PORT))
    ])


# ============================================================
# TCP SERVER
# ============================================================

def recv_exact(conn, length):
    """
    Receive exactly the requested number of bytes.

    TCP is a stream protocol, which means a single recv()
    call is not guaranteed to return an entire packet.

    For example, a four-byte header may arrive as:
        recv() -> 2 bytes
        recv() -> 1 byte
        recv() -> 1 byte

    This function repeatedly receives data until the requested
    length has been collected.

    Args:
        conn: Active client socket.
        length: Number of bytes required.

    Returns:
        bytes: Exactly 'length' bytes.

    Raises:
        EOFError: If the client disconnects before enough
                  bytes have been received.
    """

    chunks = bytearray()

    while len(chunks) < length:
        chunk = conn.recv(length - len(chunks))

        # An empty recv() means the remote side has closed
        # the connection.
        if not chunk:
            raise EOFError

        chunks.extend(chunk)

    return bytes(chunks)


def handle(conn, addr, port):
    """
    Handle an individual TCP client connection.

    Each accepted connection runs in its own daemon thread.

    The function performs the following operations:

        1. Identify whether the connection belongs to the gate
           or frontend service.
        2. Receive the four-byte packet header.
        3. Decode the encrypted packet length.
        4. Receive the complete encrypted packet body.
        5. Decrypt and inspect the packet.
        6. Send the appropriate initial or subsequent response.
        7. Close the connection when finished.

    Args:
        conn: Accepted client socket.
        addr: Client address tuple.
        port: Local server port receiving the connection.
    """

    # Use a readable label in diagnostic messages.
    label = "gate" if port == GATE_PORT else "front"

    log(
        f"[{label}] connected from "
        f"{addr[0]}:{addr[1]}"
    )

    # Disconnect clients that stop communicating for too long.
    conn.settimeout(30)

    # Track how many complete packets have been received.
    # The first packet has special handling on both services.
    count = 0

    try:
        while True:

            # ------------------------------------------------
            # Receive packet header
            # ------------------------------------------------
            #
            # Every packet starts with a four-byte encoded
            # length field.
            header = recv_exact(conn, 4)

            # Decode the length by reversing the XOR operation.
            length = struct.unpack(">I", header)[0] ^ MASK

            # Reject empty or excessively large packets.
            # The one-megabyte limit is a defensive sanity check.
            if length == 0 or length > (1 << 20):
                log(f"[{label}] invalid frame length: {length}")
                break

            # ------------------------------------------------
            # Receive encrypted packet body
            # ------------------------------------------------
            #
            # recv_exact() is used because TCP may split the
            # encrypted packet across multiple network reads.
            body = recv_exact(conn, length)

            # ------------------------------------------------
            # Decrypt packet
            # ------------------------------------------------
            try:
                plain = decrypt(body)

            except Exception as exc:
                log(f"[{label}] decrypt failed: {exc}")
                continue

            count += 1

            log(
                f"[{label}] packet {count}: "
                f"{length} encrypted bytes"
            )

            # Display the decoded packet structure when possible.
            # Unknown packets still have their raw bytes logged.
            log(f"[{label}] {describe(plain)}")

            # =================================================
            # GATE SERVER
            # =================================================
            if port == GATE_PORT:

                # The first gate packet triggers the redirect
                # response containing the frontend host and port.
                if count == 1:
                    conn.sendall(frame(gate_reply()))
                    log("[gate] sent 0x21FE gate reply")

                # No additional gate request types have been
                # implemented yet.
                else:
                    log("[gate] additional packet; no response configured")

            # =================================================
            # FRONTEND SERVER
            # =================================================
            elif port == FRONT_PORT:

                # The first frontend packet receives the
                # version-valid response.
                #
                # This is handled separately from the normal
                # request dispatcher because it is part of the
                # initial connection handshake.
                if count == 1:
                    conn.sendall(frame(make_reply(REQ_VERSION_VALID)))
                    log("[front] sent 0x219F version-valid response")

                # All subsequent packets are passed to the
                # frontend request dispatcher.
                else:
                    handle_front_request(conn, count, plain)

    # --------------------------------------------------------
    # Connection termination and error handling
    # --------------------------------------------------------

    except EOFError:
        # The client disconnected cleanly before another
        # complete packet could be received.
        log(f"[{label}] client closed after {count} packet(s)")

    except socket.timeout:
        # No incoming data arrived within the configured
        # thirty-second socket timeout.
        log(f"[{label}] idle timeout")

    except OSError as exc:
        # Covers socket-related errors such as connection resets.
        log(f"[{label}] socket error: {exc}")

    except Exception as exc:
        # Catch unexpected errors so that one client thread
        # does not terminate the entire server process.
        log(f"[{label}] handler error: {exc}")

    finally:
        # Always release the client socket when the handler exits.
        try:
            conn.close()
        except OSError:
            pass


def serve_tcp(port):
    """
    Start and maintain a TCP server on the specified port.

    Each incoming connection is handed to a separate daemon
    thread, allowing multiple clients to connect concurrently.

    Args:
        port: TCP port to listen on.
    """

    # Create an IPv4 TCP socket.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Allow the port to be reused after a previous server
    # instance has terminated.
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        # Bind the socket to the configured local address.
        server.bind((HOST, port))

    except OSError as exc:
        log(f"[{port}] listen failed: {exc}")
        return

    # Allow up to ten pending connections in the accept queue.
    server.listen(10)

    label = "gate" if port == GATE_PORT else "front"
    log(f"[{port}] listening ({label})")

    # Continuously accept incoming client connections.
    while True:
        conn, addr = server.accept()

        # Give every connection its own handler thread.
        #
        # Daemon threads do not prevent the main process from
        # exiting when the application shuts down.
        threading.Thread(
            target=handle,
            args=(conn, addr, port),
            daemon=True
        ).start()


# ============================================================
# HTTP PATCH SERVER
# ============================================================

class HTTPHandler(BaseHTTPRequestHandler):
    """
    Handle HTTP requests for game patch/resource files.

    The server exposes files beneath GAME_DIR through URLs
    beginning with /yo3/.

    Example:
        GET /yo3/example.dat

    Resolves to:
        GAME_DIR/example.dat

    Requests for files outside the game directory are rejected.
    """

    # HTTP/1.1 is used for client compatibility.
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        """
        Disable BaseHTTPRequestHandler's default logging.

        All HTTP activity is instead recorded through the
        shared log() function for consistent timestamps.
        """
        pass

    def do_GET(self):
        """
        Process an HTTP GET request.

        The requested path is checked, resolved against the
        game directory, and served if the target file exists.

        Returns:
            HTTP 200 with the file contents when found.
            HTTP 404 when the requested file is unavailable.
        """

        # Remove the query string before resolving the path.
        #
        # For example:
        # /yo3/file.dat?version=1
        # becomes:
        # /yo3/file.dat
        path = self.path.split("?", 1)[0]

        log(f"[HTTP] GET {self.path}")

        body = None

        # Resolve the root directory to its canonical absolute
        # path. This helps prevent path traversal attacks.
        root = os.path.realpath(GAME_DIR)

        # Only serve requests inside the /yo3/ namespace.
        if path.startswith("/yo3/"):

            # Remove the URL prefix to obtain the relative
            # filename inside the game directory.
            relative = path[len("/yo3/"):]

            # Resolve the requested file to its canonical path.
            full = os.path.realpath(
                os.path.join(root, relative)
            )

            # Ensure the resolved path remains inside GAME_DIR.
            #
            # realpath() resolves directory traversal sequences
            # and symbolic links before this comparison.
            #
            # The separator check prevents paths such as
            # /game-directory-other/file from passing merely
            # because they share the same textual prefix.
            if (
                full.startswith(root + os.sep)
                and os.path.isfile(full)
            ):
                # Read the requested resource as raw bytes.
                # This preserves binary game assets without
                # attempting text decoding or modification.
                with open(full, "rb") as f:
                    body = f.read()

        # A missing or invalid resource produces HTTP 404.
        status = 200 if body is not None else 404

        if body is None:
            body = b"not found"

        # ----------------------------------------------------
        # Send HTTP response
        # ----------------------------------------------------

        self.send_response(status)

        # Tell the client the exact response body size.
        self.send_header("Content-Length", str(len(body)))

        # Explicitly close the connection after this response.
        self.send_header("Connection", "close")

        self.end_headers()

        # Send the file contents or the not-found message.
        self.wfile.write(body)

        log(
            f"[HTTP] response {status}, "
            f"{len(body)} bytes"
        )

        # Prevent the HTTP/1.1 connection from being reused.
        self.close_connection = True


# ============================================================
# MAIN
# ============================================================

def main():
    """
    Application entry point.

    Startup sequence:

        1. Log configuration and directory information.
        2. Check for expected game files.
        3. Start the gate TCP server.
        4. Start the frontend TCP server.
        5. Start the HTTP patch server.
        6. Keep the HTTP server running until interrupted.

    The two TCP servers run in background daemon threads.
    The HTTP server runs in the main thread.
    """

    # --------------------------------------------------------
    # Startup information
    # --------------------------------------------------------

    log("=" * 60)
    log(f"log file: {LOG}")
    log(f"server directory: {HERE}")
    log(f"game directory: {GAME_DIR}")

    # Check for the main game executable.
    # The HTTP server can still start without it, but its
    # absence may indicate an incomplete installation.
    if not os.path.isfile(os.path.join(GAME_DIR, "yo3.exe")):
        log("[WARNING] yo3.exe not found")

    # Check that the index file still exists.
    # It should normally have been found by find_game_dir().
    if not os.path.isfile(os.path.join(GAME_DIR, "yo3.idx")):
        log("[WARNING] yo3.idx not found")

    # --------------------------------------------------------
    # Start TCP services
    # --------------------------------------------------------

    # Launch both TCP listeners independently.
    #
    # serve_tcp() contains an infinite accept loop, so each
    # listener must run in its own thread.
    for port in (GATE_PORT, FRONT_PORT):
        threading.Thread(
            target=serve_tcp,
            args=(port,),
            daemon=True
        ).start()

    # --------------------------------------------------------
    # Start HTTP patch server
    # --------------------------------------------------------

    # ThreadingHTTPServer allows multiple HTTP clients to
    # request resources concurrently.
    http = ThreadingHTTPServer(
        (HOST, HTTP_PORT),
        HTTPHandler
    )

    log(f"[{HTTP_PORT}] listening (HTTP)")

    # --------------------------------------------------------
    # Main server loop
    # --------------------------------------------------------

    try:
        # Keep processing HTTP requests indefinitely.
        # The TCP services continue running in their own threads.
        http.serve_forever()

    except KeyboardInterrupt:
        # Allow a normal shutdown using Ctrl+C.
        log("stopping server")

    finally:
        # Release the HTTP listening socket.
        http.server_close()


# ============================================================
# SCRIPT ENTRY POINT
# ============================================================

# Only execute main() when this file is run directly.
#
# This prevents the servers from starting automatically if
# the file is imported as a module by another Python script.
if __name__ == "__main__":
    main()
