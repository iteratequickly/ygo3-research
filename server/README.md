
# YGO3 Private Server — Features

## Overview

A private server implementation for Yu-Gi-Oh! Online 3 (YGO3), providing the game systems and backend functionality needed to support the client.

This README documents the server's features and internal systems only.

## Server Features

### Game Data
- Loads game data from the YGO3 data archives.
- Reads item and catalogue information, including `yo3pbininfo.bin`.
- Loads avatar items, duel disks, protectors, and sleeves.
- Loads card database information from `cards.cdb`.
- Supports game assets and associated data files.

### Player Accounts and Data
- Player account and profile data.
- Persistent player state.
- Deck storage and management.
- Player-owned cards and items.
- In-game currency and BP management.

### Card and Deck Systems
- Card database integration.
- Card lookup and search.
- Player card collections.
- Deck creation and storage.
- Card quantity management.

### Shop Systems

#### Avatar Shop
- Sends avatar shop catalogue data.
- Includes avatar items and supported duel disk catalogue entries.
- Uses the Avatar Shop response `0x1C2A`.

#### Protector Shop
- Sends protector and sleeve catalogue data.
- Includes supported duel disk entries where applicable.
- Uses the Protector Shop response `0x1C32`.

#### Duel Disk Catalogue
- Loads duel disk item information from `data/extra/yo3pbininfo.bin` inside `data002.ypk`.
- Builds duel disk catalogue entries from the loaded game data.
- Uses the configured item type and cost values in the server implementation.

**Note:** Finding duel disk image files does not automatically mean those disks are registered as valid shop catalogue entries. The catalogue data must also be loaded and recognised by the server.

### Duelling
- Duel-related game logic.
- OCG engine integration.
- Card database and script integration.
- NPC duel support.

### NPC Systems
- NPC duel functionality.
- Optional AI-assisted NPC responses using LM Studio.

### Social and Player Systems
- Player-related social functionality.
- Trading support.
- Event-related systems.

### Administration
- BP granting.
- Card granting.
- Card database search.
- Game data validation and diagnostic output.

## Optional AI Integration

The server includes optional LM Studio integration for AI-assisted NPC functionality.

AI integration is optional and is not required for the standard game systems.

## Persistent Data

The server stores persistent state in:

`data/ygo3_state.pkl`

Back up this file before making changes to persistent player data or server state.

## Disclaimer

Yu-Gi-Oh! is a trademark of Konami. This is an independently developed private server project and is not affiliated with or endorsed by Konami.