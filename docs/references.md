# YGO3 – References

This document lists external references used for research into
**Yu-Gi-Oh! Online 3: Duel Accelerator**, including its assets, file formats,
community documentation, and server cryptography.

These resources are provided for historical and technical research. They are
not necessarily official documentation, and information obtained from
community posts should be independently verified against the game files and
server implementation.

---

## 1. Game assets and file formats

### VG Resource – Yu-Gi-Oh! Online Duel Accelerator MDL files

**Link:**  
https://archive.vg-resource.com/post-658972.html

**Subject:** Game model files and asset conversion.

**Relevance:**
- Discussion of character models extracted from Yu-Gi-Oh! Online 3.
- Investigation of the game's `.mdl` model format.
- Information about the `YGO3` file header.
- Community discussion of extracting and converting models.
- References to tools used to convert extracted models into FBX format.

---

## 2. Game history and community information

### GameFAQs – Yu-Gi-Oh! Online 3: Duel Accelerator Message Board

**Link:**  
https://gamefaqs.gamespot.com/boards/981343-yu-gi-oh-online-3-duel-accelerator

**Subject:** Historical player discussions and game information.

**Relevance:**
- Historical discussions about Yu-Gi-Oh! Online 3.
- Player experiences with NPC duels and game functionality.
- Information about Booster Pass points and the in-game economy.
- Discussions about events, cards, decks, and game features.
- Reports of bugs and compatibility problems.
- Historical information about the game's service and shutdown.

---

## 3. Game file research and reverse engineering

### ZenHAX – Yu-Gi-Oh! Online 3 discussion

**Link:**  
https://zenhax.com/viewtopic.php@t=8318.html

**Subject:** Game file formats and reverse-engineering research.

**Relevance:**
- Community research associated with Yu-Gi-Oh! Online 3.
- Potential reference for examining game resources and proprietary formats.
- Useful as a lead when investigating undocumented game data.

---

## 4. Server cryptography

### Elitepvpers – [Release] YO Gi Oh Online 3 Cryptography

**Link:**  
https://www.elitepvpers.com/forum/private-server/2655667-release-yo-gi-oh-online-3-cryptography.html

**Subject:** YGO3 network cryptography and private-server research.

**Relevance:**
- Historical research relating to YGO3 cryptography.
- A potential reference for understanding client/server communication.
- Useful when investigating packet encryption, connection handling, and
  protocol implementation.

---

## 5. Research and verification guidelines

When using these references for the YGO3 project:

1. Prefer direct inspection of the original game files when determining file
   formats, packet structures, and data layouts.
2. Treat forum posts as research leads rather than guaranteed technical facts.
3. Verify packet IDs and field layouts against the Python server implementation
   and captured client/server traffic.
4. Keep asset-format research separate from network-protocol research.
5. Document any inferred behavior and distinguish it from behavior confirmed
   through testing.
6. Record the original source URL when incorporating findings into project
   documentation.
7. Do not assume that a packet ID, encryption method, or file-format detail
   applies to every version of the game without verification.

---

## 6. Source summary

| Source | Main research area | Source type |
|---|---|---|
| VG Resource | Character models and asset conversion | Community technical discussion |
| GameFAQs | Game history, NPCs, features and player reports | Community message board |
| ZenHAX | Game file research and reverse engineering | Technical discussion forum |
| Elitepvpers | YGO3 cryptography | Private-server research forum |

---

*Last reviewed: 2026-11-11*

*This document is an index of external research references, not a claim of
ownership or authorship of the linked material.*
