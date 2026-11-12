<div align="center">

# Yu-Gi-Oh! ONLINE 3 — Private Server

**An experimental preservation project for *Yu-Gi-Oh! ONLINE 3: Duel Accelerator***

[![MIT License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Downloads](https://img.shields.io/github/downloads/iteratequickly/ygo3-research/total.svg?label=Downloads)](https://github.com/iteratequickly/ygo3-research/releases)
[![GitHub Release](https://img.shields.io/github/v/release/iteratequickly/ygo3-research)](https://github.com/iteratequickly/ygo3-research/releases)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![Status](https://img.shields.io/badge/Status-Work%20in%20Progress-orange.svg)](#project-status)

<img src="https://raw.githubusercontent.com/iteratequickly/ygo3-research/refs/heads/main/assets/image8.png" alt="Yu-Gi-Oh! ONLINE 3" width="720">

[Status](#project-status) · [Quick Start](#quick-start) · [Private Server](#part-1--experimental-private-server) · [YPK Extractor](#part-2--ypk-archive-extractor) · [Known Issues](#known-bugs) · [Troubleshooting](#troubleshooting)

</div>

---

## About

This project combines two tools:

| Component | Description | Main file(s) |
|---|---|---|
| 🖥️ **Experimental Private Server** | A local Python server that reproduces selected parts of the original game's server infrastructure, with EDOPro-powered duel support. | `ygo3_server.py`, `ygo3_gui.py` |
| 📦 **YPK Archive Extractor** | A graphical tool for extracting `.ypk` archives from the Yu-Gi-Oh! ONLINE series. | `ypk_extractor.py` |

The goal is to investigate the original game's network communication, understand its data formats, access its resources, and support further preservation and community development.

## Screenshots

### Private Server Progress

| Main Menu | Login Screen |
|:---:|:---:|
| <img src="https://raw.githubusercontent.com/iteratequickly/ygo3-research/refs/heads/main/assets/image2.png" alt="Main Menu"> | <img src="https://raw.githubusercontent.com/iteratequickly/ygo3-research/refs/heads/main/assets/image3.png" alt="Login Screen"> |

| Player Selection | Client State |
|:---:|:---:|
| <img src="https://raw.githubusercontent.com/iteratequickly/ygo3-research/refs/heads/main/assets/image4.png" alt="Player Selection"> | <img src="https://raw.githubusercontent.com/iteratequickly/ygo3-research/refs/heads/main/assets/image7.png" alt="Client State"> |

### YPK Extractor

<div align="center">
<img src="https://raw.githubusercontent.com/iteratequickly/ygo3-research/refs/heads/main/assets/image5.png" alt="YPK Extractor" width="640">
</div>

---

## Project Status

> [!WARNING]
> **Work in progress.** This project is experimental and incomplete. There is no guarantee that a fully playable offline version of the original game can be achieved.

- The private server has progressed beyond initial connection handling and now contains server-side components for **account, player, card, shop, and duel** functionality.
- Full end-to-end compatibility with the original client has **not** been established.
- The original milestone was getting the client to connect to a local Python server, pass the initial login flow, and receive test account and player-list data. At that stage, the client reached a **black screen after player selection**.
- Further duel and game-system development has been added since, but a complete playable offline experience has not been verified.
- The YPK extractor supports recognized archive entries, subject to the implemented archive format and compression methods.

## Game Download

The original installer is available on the Internet Archive:

**[Download Yu-Gi-Oh! ONLINE 3 (2010)](https://archive.org/details/yo3setup_20100709en)**

> [!NOTE]
> The download is hosted by the Internet Archive, not by this project. Always verify downloaded files and keep a backup of your original game installation.

---

## Quick Start

1. **Install the dependencies**
   ```bash
   py -3.12 -m pip install pycryptodome customtkinter
   ```
2. **Gather the EDOPro components** (`ocgcore.dll`, `cards.cdb`, Lua scripts) from [Project Ignis](https://projectignis.github.io/download.html). Architecture must match your Python (32-bit ↔ 32-bit, 64-bit ↔ 64-bit).
3. **Back up** your original `yo3.ini`, then point it at `127.0.0.1` (see [Local Server Configuration](#local-server-configuration)).
4. **Launch the GUI** and configure paths:
   ```bash
   py ygo3_gui.py
   ```
5. **Start the server**, launch the game client, leave the login fields **blank**, and press **OK**.

Just want the archive tool? Run `py ypk_extractor.py` — it needs only the standard library and Tkinter.

---

## Table of Contents

- [Part 1 – Experimental Private Server](#part-1--experimental-private-server)
  - [How It Works](#how-it-works)
  - [Protocol Details](#protocol-details)
  - [Test Account & Login](#test-account--login)
  - [Duel System](#duel-system)
  - [Configuration](#configuration)
  - [Server Control GUI](#server-control-gui)
  - [Running the Server](#running-the-server)
  - [Requirements](#requirements)
- [Part 2 – YPK Archive Extractor](#part-2--ypk-archive-extractor)
- [Part 3 – Preservation & Community Development](#part-3--preservation--community-development)
- [Known Bugs](#known-bugs)
- [Private Server Limitations](#private-server-limitations)
- [Troubleshooting](#troubleshooting)
- [Disclaimer & License](#disclaimer)

---

# Part 1 – Experimental Private Server

An experimental Python implementation that reproduces selected parts of the original Yu-Gi-Oh! ONLINE 3 server infrastructure. It investigates how the original client communicates with its servers and provides local responses for supported requests, using separate listeners for each communication service.

### Progress so far

The initial connection and login research established that:

- [x] The original client can be launched
- [x] The client reaches the main menu and login screen
- [x] The client connects to the local Python server
- [x] The server responds to initial authentication and profile requests
- [x] The server supplies test account information and player-list responses
- [x] The client proceeds to player selection
- [ ] Full original-client compatibility *(still requires testing and compatibility work)*

Additional server-side functionality (duel-engine integration, game-system components) has since been developed.

## How It Works

The server uses observations of the original client's network traffic to implement selected protocol operations. Current research covers:

| Area | Area |
|---|---|
| Gate server communication | Duel server communication |
| Front server communication | HTTP resource requests |
| Authentication & profile responses | Card and deck data |
| Login handling | Game state management |
| Player-list handling & selection | Duel-engine integration |

> [!NOTE]
> Not every packet field or protocol behaviour has been fully confirmed. Some responses and values remain experimental or inferred.

## Protocol Details

### Supported request types

| Request | Response | Purpose |
|:---:|:---:|---|
| `0x1F47` | `0x219A` | Authentication / profile |
| `0x1F48` | `0x2201` | Login / profile |
| `0x1F49` | `0x2260` | Player list |
| `0x1F4C` | `0x2266` | Player selection |

Additional observed responses:

| Response | Purpose |
|:---:|---|
| `0x21FE` | Gate response |
| `0x219F` | Front version validation |

These identifiers describe the currently researched protocol paths and are not proof of complete protocol compatibility.

### Packet format

The original client uses an encrypted packet format. The experimental implementation includes:

| Property | Value |
|---|---|
| Cipher | Blowfish, ECB mode |
| Key (8 bytes) | `12345678` |
| Length mask | `0x77777777` |
| Word handling | Four-byte word swapping |
| Alignment | Eight-byte blocks |
| Payload encoding | Custom nested map/list data |

These details are based on protocol observations and may not cover every packet type or edge case.

## Test Account & Login

The test setup uses placeholder data, not connected to any original online accounts.

| Field | Test Value |
|---|---|
| User ID | `1001` |
| Username | `Duelist` |
| Regulation | `1` |
| Duel Points | `0` |
| Player Index | `0` |
| Duel Level | `1` |

**To log in:**

1. Start the local server.
2. Launch the original game client.
3. Open the login screen.
4. Leave the user ID and password fields **blank**.
5. Press **OK** and let the server supply the placeholder account.

The player selection screen also uses placeholder player information.

## Server Features

Gate, Front, and Duel server communication · HTTP resource serving · test account and profile handling · player-list responses and selection · card database access · deck-related data · BP and card management · persistent server state · duel management · EDOPro OCG core integration.

Availability and completeness of each feature depend on the current implementation and original client compatibility.

## Duel System

Experimental duel functionality is powered by the **EDOPro OCG engine** from Project Ignis. The server loads the OCG core through Python's native-library interface for rules and duel processing.

**[Download EDOPro (Project Ignis)](https://projectignis.github.io/download.html)**

> [!NOTE]
> The download is hosted by Project Ignis, not by this project. Make sure the `ocgcore.dll` you use matches your Python architecture (see below).

**Required components**

| File | Purpose |
|---|---|
| `ocgcore.dll` | OCG core engine |
| `cards.cdb` | Card database (compatible with the OCG core) |
| `constant.lua`, `utility.lua` (+ other scripts) | Lua scripts required for duel setup and card effects |

<details>
<summary><b>Architecture compatibility</b></summary>

Python and `ocgcore.dll` must use matching architectures.

| Python | OCG core |
|---|---|
| 32-bit | 32-bit DLL |
| 64-bit | 64-bit DLL |

64-bit Python is supported when the corresponding 64-bit OCG core is available. The GUI's 32-bit interpreter detection and check options are conveniences for the standard 32-bit setup — they do not restrict the project to 32-bit Python.

</details>

<details>
<summary><b>Duel data conversion, timers, and NPC behaviour</b></summary>

**Data conversion** — The duel manager converts YO3 card data to the OCG engine's identifiers, including YO3 big-endian 16-bit card rows into OCG card passcodes.

**Timers** — Server-side settings; their presentation in the original client remains subject to compatibility testing.

| Timer | Duration |
|---|---:|
| Turn timer | 180 seconds |
| Total game timer | 600 seconds |

**NPC attack logic** (experimental, not a recreation of the original AI):

1. Perform a legal direct attack when permitted.
2. Otherwise, attack an opposing monster that can legally be attacked.
3. End the Battle Phase when no legal attack is available.

</details>

## Configuration

Paths for the game installation, OCG core, card database, Lua scripts, persistent data, and web resources are configurable.

| Default | Value |
|---|---|
| Server data path | `data/yo3_state.pkl` |
| Web directory | `www` |

### Environment variables

| Variable | Purpose |
|---|---|
| `YGO3_GAME_DIR` | Original game directory |
| `YGO3_DLL_PATH` | OCG core DLL path |
| `YGO3_CARD_DB` | Card database path |
| `YGO3_SCRIPT_DIR` | Lua script directory |
| `YGO3_DATA_DIR` | Persistent data directory |
| `YGO3_WEB_DIR` | HTTP web root |
| `YGO3_HOST` | Server bind address |
| `YGO3_FRONT_ADDRESS` | Front server address |
| `YGO3_GATE_PORT` | Gate server port |
| `YGO3_FRONT_PORT` | Front server port |
| `YGO3_DUEL_PORT` | Duel server port |
| `YGO3_HTTP_PORT` | HTTP server port |

### Original game configuration (`yo3.ini`)

> [!IMPORTANT]
> Back up the original `yo3.ini` before making changes.

The original configuration may contain entries similar to:

```ini
[Yo3]
GateServerAddress=yo3gate01.yugioh-online.net
GateServerPort=5730
PatchUpdateUrl=http://www.yugioh-online.net/yo3/
PatchDataUrl=http://dl.ygoda.konamionline.com/ygoda/patch/
InfoURL=http://www.yugioh-online.net/app/
```

### Local server configuration

For local experimentation, change it to:

```ini
[Yo3]
GateServerAddress=127.0.0.1
GateServerPort=5730
PatchUpdateUrl=http://127.0.0.1/yo3/
PatchDataUrl=http://127.0.0.1/ygoda/patch/
InfoURL=http://127.0.0.1/app/
```

The HTTP server supports the relevant route structure, but `/ygoda/patch/` and `/app/` are not yet complete replacements for the original services. `/yo3/` is the primary implemented resource service.

### Local network defaults

| Service | Address | Port |
|---|---|---:|
| Gate server | `127.0.0.1` | `5730` |
| Front server | `127.0.0.1` | `1213` |
| Duel server | Configurable | Configurable |
| HTTP server | `127.0.0.1` | `80` |

Ports can be changed via the GUI or environment variables.

> [!NOTE]
> Port 80 may require administrator privileges on Windows or may already be in use by another application.

> [!CAUTION]
> This configuration is for local experimentation only. Do not expose the experimental server directly to the public internet.

### HTTP file service

The primary route is `/yo3/` — for example `http://127.0.0.1/yo3/yo3.idx`. The service looks up requested files inside the detected game directory, supports `GET` and `HEAD`, and includes path-traversal protection.

| Status | Meaning |
|:---:|---|
| `200` | Requested file exists |
| `403` | Blocked or unsafe path |
| `404` | File not found |
| `405` | Unsupported request method |

---

## Server Control GUI

The graphical launcher, built with CustomTkinter, is the recommended way to configure and manage the server.

```bash
py ygo3_gui.py
```

It has three tabs: **Server Settings**, **BP Manager**, and **Server Logs**.

### Server Settings

Configure the server script, game directory, OCG core DLL, `cards.cdb`, Lua script directory, persistent data directory, HTTP web directory, Python interpreter, bind address, front server address, and the Gate / Front / Duel / HTTP ports. Settings are stored in `ygo3_launcher_config.json`, making it easy to switch between installations and environments.

The interpreter selector includes a **Find 32-bit** option and a **Python environment check** that verifies the 32-bit setup and tests whether `Crypto.Cipher.Blowfish` imports. A matching 64-bit Python and OCG core also work. The GUI provides **Start Server**, **Stop Server**, and a status display.

### BP Manager

Change a test account's BP balance by entering a **User ID** and **BP amount**. The GUI calls:

```bash
py ygo3_server.py --grant-bp AMOUNT USER_ID
```

Validation covers positive BP amounts, non-negative user IDs, the maximum 32-bit unsigned BP value, and remaining BP capacity. The manager also accounts for the server's persistent state.

> [!IMPORTANT]
> Stop the running server before changing BP. The GUI attempts to restart the server afterwards so an old in-memory state doesn't overwrite the updated data.

### Server Logs

Captures stdout, stderr, live log output, and process status/exit code, with a button to clear the display. Useful for debugging connections, packet handling, HTTP requests, OCG initialisation, and errors. Logs may include incoming connections, received and decrypted packets, parsed fields, server responses, and exceptions. The original research setup also used `gate_test.log`.

---

## Running the Server

```bash
py ygo3_gui.py          # recommended: GUI launcher
py ygo3_server.py       # start the server directly
```

| Command | Purpose |
|---|---|
| `py ygo3_server.py --check-game` | Check the game installation |
| `py ygo3_server.py --card-search` | Search the card database |
| `py ygo3_server.py --grant-bp AMOUNT USER_ID` | Grant BP |
| `py ygo3_server.py --grant-card` | Grant a card (uses the server's configured data paths) |

## Requirements

| Requirement | Details |
|---|---|
| **OS** | Windows is the primary target; others may need extra configuration |
| **Python** | 3.12 (documented development version); architecture must match `ocgcore.dll` |
| **Server & GUI** | `pycryptodome`, `customtkinter` |
| **Duel system** | `ocgcore.dll`, `cards.cdb`, Lua scripts (`constant.lua`, `utility.lua`) |
| **YPK extractor** | Standard library + Tkinter only |

```bash
py -3.12 -m pip install pycryptodome customtkinter
```

If you use a different interpreter, install the dependencies into that interpreter. The Blowfish implementation requires `from Crypto.Cipher import Blowfish`.

---

# Part 2 – YPK Archive Extractor

A graphical tool for extracting supported `.ypk` archives from the Yu-Gi-Oh! ONLINE series — it is **not limited to ONLINE 3**. It is intended for preservation, resource inspection, and research across releases.

## Features

- Select one or multiple `.ypk` archives and choose an output directory
- Extract recognized entries while preserving internal folder structure
- Support for stored and raw DEFLATE entries, with automatic decompression
- File size and CRC32 validation
- Separate output folder per archive
- Progress display and extraction log
- Original archives are never modified
- No hardcoded game installation paths

## Usage

```bash
py ypk_extractor.py
```

1. Select one or more YPK archives.
2. Choose an output directory.
3. Start extraction.
4. Monitor progress and log output.
5. Open the generated output folders.

## Archive Format

The extractor recognises the signature `YO\x03\x04` and reads: compression method, CRC32, compressed size, uncompressed size, filename length, extra field length, filename, and compressed payload.

| Method | Description | Support |
|:---:|---|---|
| `0` | Stored / uncompressed | ✅ Supported |
| `8` | Raw DEFLATE | ✅ Supported |
| Other | Other compression methods | ❌ Not currently supported |

Extracted data is checked against the recorded size and CRC32 where applicable.

> [!NOTE]
> The extractor is designed to be reusable across the series, but universal compatibility with every YPK variation has not been established.

---

# Part 3 – Preservation & Community Development

Extracted resources may support future research and preservation projects, such as custom clients, rebuilt user interfaces, asset viewers and editors, alternative resource systems, alternative server implementations, and multi-game preservation efforts. Extracting resources alone does **not** create a playable recreation of the game.

**Potential areas for contribution** *(not claims of completed features)*:

- Completing the original client connection flow
- Improving protocol compatibility
- Expanding account and player management
- Improving duel integration
- Implementing missing game services
- Improving card and deck handling
- Expanding YPK format compatibility
- Developing resource inspection tools
- Investigating alternative client implementations

---

# Known Bugs

| Issue | Description |
|---|---|
| Eagle selection crash | Selecting Eagle causes the client to crash |
| Special Packs | Not visible in the shop |

These issues remain outstanding and require further investigation.

## Private Server Limitations

The server does not currently guarantee complete compatibility with the original game. Incomplete or unverified areas include:

- Full account authentication and profile management
- Player and lobby management
- Duel rooms, matchmaking, and session handling
- Card and deck synchronisation
- Original-client duel gameplay
- Shop functionality and game item handling
- Patch and information services
- Compatibility with the original online infrastructure

Some profile and player-list values are placeholders, and some protocol fields have been inferred rather than confirmed. Client crashes and unexpected behaviour may occur with unimplemented features.

---

# Troubleshooting

<details>
<summary><b>The server does not start</b></summary>

Check that Python is installed, the correct interpreter is selected, dependencies are installed, the server script exists, and the configured ports are available.

</details>

<details>
<summary><b>Blowfish import error</b></summary>

Install PyCryptodome into the environment used by the server, then test the import:

```bash
py -3.12 -m pip install pycryptodome
py -3.12 -c "from Crypto.Cipher import Blowfish; print('Blowfish OK')"
```

</details>

<details>
<summary><b>OCG core fails to load</b></summary>

Check that `ocgcore.dll` exists, its architecture matches Python (32-bit ↔ 32-bit, 64-bit ↔ 64-bit), it's compatible with your EDOPro files, and the configured path is correct.

</details>

<details>
<summary><b>Card database or Lua script problems</b></summary>

Check that `cards.cdb` exists, the GUI points to it, and it's compatible with the OCG core. Confirm the script directory contains `constant.lua` and `utility.lua`.

</details>

<details>
<summary><b>HTTP resource errors</b></summary>

Check that the game directory and web root are correct, the requested resource exists, the HTTP port is available, and the route is implemented. Remember that `/ygoda/patch/` and `/app/` are not yet complete replacements for the original services.

</details>

<details>
<summary><b>The original client gets stuck (black screen / stall)</b></summary>

The server is experimental. A stalled client may indicate an unsupported packet, an incomplete server response, missing game resources, an incorrect client configuration, an unimplemented service, or an unexpected client-side dependency. Check the server logs for connection activity, packet handling, and errors.

</details>

---

# Original Game Files

This project is for preservation and research. The YPK extractor reads archives and writes extracted resources to a separate output directory — it does not intentionally modify the originals. Always back up original game files and configuration before experimenting.

# Disclaimer

This is an independent fan-made research and preservation project. It is not affiliated with, endorsed by, or supported by Konami. Yu-Gi-Oh! and related names, trademarks, characters, artwork, and game assets belong to their respective rights holders.

This project is provided **as-is**, without warranties of any kind. You are responsible for ensuring that your use of the software and any extracted resources complies with applicable laws and the rights of the relevant intellectual property owners.

# License

Released under the [MIT License](LICENSE).
