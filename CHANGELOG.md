# Verze Rádia Beat

Číslování jako u Handheld Control: 0.MINOR.PATCH. Každá změna (commit) je nová verze: PATCH o jedna víc; větší novinka zvýší MINOR (PATCH znovu od 0). Starší verze očíslovány zpětně.

## 0.28 — Společné vylepšování obrázků

- **0.28.3** (2026-11-05) Data vydání: „1. ledna“ byla jen náhražka služeb za neznámý den (U2 The Joshua Tree vyšlo 9. března 1987) a „1970“ Deezerovo „nevím“: teď rok sám, nebo přesné datum odjinud; 159 takových přehrání se dohledá znovu. MusicBrainz najde i „Mr.“ místo „Mister“ a název s „The“, živé nahrávky se nepočítají za první vydání
- **0.28.2** (2026-11-05) Oprava vzorů, které si AI vymyslí (čáry a mřížky místo jemné textury): nabídka v nastavení Vypnuto / Slabá / Střední / Silná, výchozí Silná; přepočítá se jen sama oprava, bez AI, a platí i pro už hotové obrázky
- **0.28.1** (2026-11-04) Staré obaly se vylepšují i při otevřeném okně: počítá se jen GPU, kterou používají jiné programy (vlastní sklo a plocha ne), jinak by se při celé obrazovce nedostaly na řadu
- **0.28.0** (2026-11-04) Obrázek na obrazovce má vždy přednost (přeruší malé a staré obrázky, ty pak běží znovu); staré obaly z historie se vylepšují na pozadí, jen když je GPU volná, procesor v klidu a počítač není na baterii; počítače se přes sdílenou databázi domluví, kdo který obrázek dělá, a hotový si předají hned

## 0.27 — Obrázky všech dnů

- **0.27.7** (2026-11-04) Prevence zamrznutí: na integrovaných GPU běží AI zvětšování vždy po nejmenších dlaždicích a po selhání se už nikdy nevrací k větším (dlouhé úlohy GPU vypršely a jeden takový výpadek skončil resetem celé GPU)
- **0.27.6** (2026-11-04) Tlačítko Zkontrolovat je stejná červená pilulka jako časy v HRÁLO; místo pro počasí pod myší se zvýrazní stejnou červenou
- **0.27.5** (2026-11-04) Počasí i Měsíc se vypnou klepnutím na obrázek a zapnou klepnutím na jeho prázdné místo (pod myší je tam slabý obrázek); přepínače v nastavení to sledují
- **0.27.4** (2026-11-04) Místo pro počasí: seznam se rozbalí přímo pod vybraným místem ve skle panelu, s celými názvy (žádná šedá nabídka na druhé straně)
- **0.27.3** (2026-11-04) Po resetu GPU (VK_ERROR_DEVICE_LOST) okno nezamrzne: Rádio Beat se samo spustí znovu; když reset způsobilo AI zvětšování (Real-ESRGAN), dál jen nejmenší dlaždice (kratší úlohy, GPU nevyprší)
- **0.27.2** (2026-11-03) Samurajské masky vždy červené (žádné černé)
- **0.27.1** (2026-11-03) Den památky obětí holokaustu: kromě svíček, holubic a bílých srdcí padá zlatá Davidova hvězda (kreslená)
- **0.27.0** (2026-11-03) Každý den z kalendáře (499 mezinárodních a významných dnů) má vlastní padající obrázky (barevné emoji: štěňata, jazz, špagety, Harry Potter…), napůl s ozdobami ročního období jako svátky; pietní dny (oběti, genocidy, otroctví, násilí) tiché obrázky: svíčky, holubice, bílé květy a stužky; v okénku dne jsou u každého dne jeho obrázky

## 0.26 — Počasí

- **0.26.9** (2026-11-03) Měsíc u tlačítka ⓘ se při úzkém místě (celá obrazovka, široký obal) zmenší, místo aby zmizel
- **0.26.8** (2026-11-03) Měsíc vlevo od tlačítka ⓘ (mezi obalem a tlačítky, nad datem): jak je teď vidět z vybraného místa, šipka a jeho příští fáze (nov, první čtvrť, úplněk, poslední čtvrť) s datem, když je místo; vypínač v nastavení
- **0.26.7** (2026-11-03) Den japonské kultury (3. listopadu) jako zvláštní den s vlastními padajícími ozdobami, napůl s podzimními: katany, samurajské masky, sakury a japonské vlajky
- **0.26.6** (2026-11-03) Počasí bez °C a ° (jen čísla a šipky, % zůstává), kapka menší: čísla větší
- **0.26.5** (2026-11-03) Čísla počasí tučná a vyšší (protažená do výšky jako informace o skladbě, šířku hlídá okraj obalu), kapka stejně vysoká jako čísla; odlesk na velkém skle panelů a nabídek výraznější (dlouhá záře vlevo nahoře, světlo vpravo dole)
- **0.26.4** (2026-11-02) Počasí větší; kapka vlhkosti je opravdové sklo jako nabídky (lom obrazu za ní, modrý nádech, okraj a odlesk); všechno sklo (tlačítka, pilulka, panely, nabídky) má realističtější odlesk: měkká záře s jasným bodem vlevo nahoře a světlo soustředěné uvnitř vpravo dole
- **0.26.3** (2026-11-02) Počasí v jednom řádku jen obrázky a čísla: obloha (v noci Měsíc ve své fázi), teplota, nejnižší ↓ a nejvyšší ↑, skleněná kapka a vlhkost v %; bez popisu, místa a názvu fáze
- **0.26.2** (2026-11-02) Počasí větší, obloha velká vlevo přes oba řádky; Měsíc kreslený jako skutečný (modrošedý, moře, krátery Tycho a Koperník s paprsky), osvětlený a natočený tak, jak ho teď vidíš z vybraného místa; fáze počítaná přesně (podle polohy Slunce a Měsíce: teď couvající srpek, ne poslední čtvrť); malý Měsíc i za názvem fáze
- **0.26.1** (2026-11-02) Počasí se opravdu ukáže vedle loga (místo pro ně se počítalo od rámečku informací o skladbě, který začíná výš než jejich písmena: vyšlo nic); v nastavení je vybrané místo vidět celé (pod nadpisem)
- **0.26.0** (2026-11-02) Počasí vedle loga vlevo nahoře (mezi logem a obalem, nad informacemi o skladbě, od horního okraje s mezerou): barevná obloha (slunce, slunce za mrakem, déšť…, v noci Měsíc v jeho fázi), teplota, nejnižší a nejvyšší za den, vlhkost, popis, místo a fáze Měsíce; místo v nastavení (Oprechtice, Ostrava-Přívoz, Ostrava-Poruba, Praha; výchozí Oprechtice); data z Open-Meteo, každých 20 minut

## 0.25 — Padající ozdoby

- **0.25.14** (2026-11-02) Přepínače v panelech a nabídkách s výrazným tmavým obrysem (dráha i kolečko): vidět i na bílém obalu
- **0.25.13** (2026-11-02) Všechno sklo čiré bez šedého závoje (i nabídky); v panelech a nabídkách bez světlých podkladů řádků, čar mezi nimi a šedých drah přepínačů; pilulka kvality se řídí Ztmavením pozadí jako ostatní tlačítka (dřív vždy naplno, skoro černá)
- **0.25.12** (2026-11-02) Sklo panelů a nabídek zase jako dřív (průhledné, Ztmavení pozadí platí i pro panely); čitelnost místo toho dává tmavá záře kolem samotných písmen a přepínačů
- **0.25.11** (2026-11-02) Panely ⓘ a ⚙ ztmavené vždy, jako nabídky: jejich text je čitelný i při Ztmavení pozadí 0 % (to dál řídí jen plochy za textem okna)
- **0.25.10** (2026-11-02) Sklo panelů ⓘ a ⚙ a nabídek tmavší, text čitelnější i na světlém obalu; padající ozdoby za sklem zase naplno
- **0.25.9** (2026-11-02) Padající ozdoby za sklem všech nabídek a panelů slabší (asi na 40 %): text v nich je čitelnější, okno mimo sklo beze změny
- **0.25.8** (2026-11-02) Opraveno rozbité vytížení GPU u posledního vylepšeného obrázku („vytížení GPU 7vytížení GPU 7 %“ místo „vytížení GPU 77 %“); už rozbité záznamy se zobrazí správně
- **0.25.7** (2026-11-02) Úspornější padající ozdoby (každý snímek spočítané jednou pro okno i všechna skla, sklo kreslí jen ty, které ukazuje, bez masek), aby stačily i na 120–165 Hz; otevření nabídky kvality a dne bez zaškobrtnutí: sklo snímá jen okolí nabídky, ne celé okno (pětina práce); GPU sklo se zahřeje hned po spuštění, ne až při prvním otevření
- **0.25.6** (2026-11-02) Nabídka kvality přesně mezi pravým okrajem obalu a pravým okrajem obrazovky (s mezerou z obou stran)
- **0.25.5** (2026-11-02) Za sklem nabídek a panelů víc padajících ozdob (padají za celým sklem, ne jen u data; k okrajům se jen pozvolna vytrácejí) a u okrajů skla zrcadlově odražené ty vedle něj, jako datum a čas; žádné cukání každou sekundu či dvě: sklo okno znovu nesnímá kvůli blikající dvojtečce času, jen když se opravdu něco změní
- **0.25.4** (2026-11-02) Padající ozdoby plynule v plné snímkové frekvenci obrazovky (60 Hz a víc), i za sklem nabídek a panelů (kreslené do skla každý snímek, zvětšené jako jeho střed); pod sklem už žádné posunuté dvojníky: tlačítka a pilulka kvality jen zrcadlově, okraje bez starých kopií
- **0.25.3** (2026-11-02) Na podzim padá hlavně javorové listí; ozdoby už se nezastaví při otevřené nabídce ani nad datem; panely ⓘ a ⚙ ukazují jako nabídka kvality i datum a čas, padající ozdoby, informace o skladbě a tlačítka za sklem, živě (až 15× za sekundu, na velkém skle 4K méně: nejvýš čtvrtina jádra CPU)
- **0.25.2** (2026-11-02) Padající ozdoby s jemnou září kolem: hnědé listí vynikne i na hnědém obalu, netopýři na tmavém
- **0.25.1** (2026-11-02) Padající ozdoby větší (asi 1,5×) a asi dvakrát víc; ozdoby ročního období padají vždy (na podzim listí, v zimě vločky…), o svátcích a zvláštních dnech napůl s jejich vlastními
- **0.25.0** (2026-11-02) Ozdoby podle ročního období (o svátcích jejich vlastní) pomalu padají jako sníh a náhodně se pootáčejí za datem a časem, tlačítky ⓘ ⚙ ✕, kvalitou, hlasitostí a jmény; písmena data a času jen v barvách měsíce, čitelnější; bez animací stojí, s otevřenou nabídkou se zastaví

## 0.24 — Sdílení přes internet (společný server)

- **0.24.1** (2026-11-01) Adresa společného serveru (https://ryzen7030pro.tail326f7d.ts.net, Tailscale Funnel): sdílení přes internet zapnuto pro všechna Rádia Beat; server neomezuje aplikaci na svém počítači, ostatní adresy až 3000 požadavků za 10 min
- **0.24.0** (2026-11-01) Společný server (radio_beat_hub.py, hub-setup.sh): vylepšené obrázky, hrané skladby a údaje o skladbách sdílené přes internet mezi všemi Rádii Beat; obrázky ze serveru se před prvním použitím porovnají s originálem

## 0.23 — Sklo počítané grafickou kartou (shader)

- **0.23.15** (2026-11-01) Poslední vylepšený obrázek: k výsledné velikosti i celková doba vylepšení (výsledný 480 KB / 1:20), u obrázků z mezipaměti „(z mezipaměti)“
- **0.23.14** (2026-11-01) Číslování verzí jako u Handheld Control, i zpětně pro všechny dřívější verze (tento seznam); rychlejší kroky vylepšování obrázků
- **0.23.13** (2026-11-01, `631b0ee`) v1.0.0: version numbering as in HHC; faster enhancer steps (light PNG compression)
- **0.23.12** (2026-11-01, `fb38d9e`) Edge mirrors: no unmirrored ghost beyond the edges
- **0.23.11** (2026-11-01, `49b6193`) Glass follows the cover's morph: old picture cross-fades into the new one in step with the cover
- **0.23.10** (2026-11-01, `56e55f3`) Day info box: bottom edge mirror stretched 2.2x and brighter so the thin slider shows
- **0.23.9** (2026-11-01, `fdeb473`) Day info box: headlines in red pills like ON AIR
- **0.23.8** (2026-11-01, `514cd75`) Day info box: text inset by the glass's corner radius on every side, as in the i panel
- **0.23.7** (2026-11-01, `ca0234a`) Day info box: fills the cover (gaps from its edges, above the slider) with fitted text; mirrors all four edges
- **0.23.6** (2026-11-01, `21ba5d2`) i and gear menus mirror the quality pill (AAC 128 kbps) at their bottom edge like the Kvalita menu
- **0.23.5** (2026-11-01, `ae68ee2`) Log: explain the harmless lsfg-vk Vulkan layer warning when that layer is installed
- **0.23.4** (2026-11-01, `ce63be9`) Log: 200 MB cap, also checked hourly while running
- **0.23.3** (2026-11-01, `c9acefc`) GPU note: say the upload total is transferred over time, not held
- **0.23.2** (2026-11-01, `153e619`) Log errors and warnings to ~/.local/state/radio-beat/radio-beat.log
- **0.23.1** (2026-11-01, `178906a`) GPU glass: fix crash when reusing freed lens textures; retry after toggling off/on
- **0.23.0** (2026-11-01, `6a53c1d`) Glass lens on the GPU: one OpenGL shader pass (libepoxy, nothing to install) for every glass instead of thousands of ring layers

## 0.22 — Jmeniny a okno s informacemi o dni

- **0.22.8** (2026-11-01, `5d98d4a`) Status (Opus 126 kbps · YouTube…, Pozastaveno) moved under the volume slider, centred, fitted to the room there and clear of the names; off the song info's panel (it pushed the title up over the logo); capped short song-info rows centred
- **0.22.7** (2026-11-01, `53df8da`) Song info: a short row (album "1", a one-word title) no taller than 1.6x its part's usual line, like the clock's one-digit day, not a giant across the room
- **0.22.6** (2026-11-01, `4bb99b2`) Clock decorations a little less crowded (between the sparse original and the full last version)
- **0.22.5** (2026-11-01, `5d377cf`) Clock fills its whole room like the song info: every line as wide as the room, squeezed or stretched in height together; names back to the full room width
- **0.22.4** (2026-11-01, `369309a`) Names as wide as the date and time lines above (unified), in the middle of their room
- **0.22.3** (2026-11-01, `5f13117`) Day info box shown by itself once an hour at :20 for 15 s (away from the panel's tours at :00 and :30); not while a panel, menu or tour is open; makes way when one opens
- **0.22.2** (2026-11-01, `0acc21d`) Names fill their whole room (as tall, then stretched as wide), like the date and the song info; the pictures on stretched letters drawn at the size seen, not stretched
- **0.22.1** (2026-11-01, `3cc5ab4`) Clock decorations: motifs placed on the letters only (none wasted in the gaps) and about four times as many: more pictures, less bare colour
- **0.22.0** (2026-11-01, `bb461d8`) Calendar: name days below the volume slider, the special day's name in the clock, a glass box on hover

## 0.21 — Svátky a významné dny

- **0.21.5** (2026-11-01, `d4aa32f`) Clock: 1–2 Nov Halloween together with Dušičky (grave lamps, candles, wreaths, chrysanthemums) and Día de los Muertos (calaveras, cempasúchil, papel picado)
- **0.21.4** (2026-11-01, `5e8c131`) Clock: one-digit days (1, 2…) as big as a two-digit day, not stretched to a giant; the other lines stay as wide as the room
- **0.21.3** (2026-11-01, `95fdfe2`) Halloween back to 31 October only (as before)
- **0.21.2** (2026-11-01, `d116e29`) Halloween on the date and time through 2 November (with Dušičky's candles and skulls on the 1st and 2nd)
- **0.21.1** (2026-10-31, `1b01485`) Picture steps (AI upscaling, sharpening, denoising) say how much memory their programs took at most: RAM, and on the GPU VRAM + GTT (from /proc and the DRM fdinfo, sampled every 0.2 s)
- **0.21.0** (2026-10-31, `893691a`) Special days on the date and time: Czech, US and world holidays (Christmas, Advent, Halloween, 4th of July stars and stripes, Czech flag on state holidays, Easter eggs, Masopust, čarodějnice, Valentine's, St Patrick's, Thanksgiving, Día de los Muertos, Chinese New Year, Diwali, Hanukkah, Star Wars day, Pi day, Earth day, Children's, Mother's, Father's day, World Radio / Rock / Music day...) with their own motifs; motifs no bigger on the big day number

## 0.20 — Ozdoby ročních období na písmenech

- **0.20.5** (2026-10-31, `72fc6cf`) Each effect's note says its memory (mirror: GPU for the mirrored strips + RAM for the lettering map; season: GPU for its once-a-day textures; shadows and animations: none of their own), and the notes show the whole app's RAM and GPU VRAM + GTT (DRM fdinfo); season decorations rendered once a day into textures (cheaper each second)
- **0.20.4** (2026-10-31, `0cd1f7e`) Menus' glass never shows an earlier picture's lens or mirror: opening the 'i' or gear menu draws it anew, and while a new picture's lens is built the current picture shows plainly magnified instead of the old lens
- **0.20.3** (2026-10-31, `c415db7`) A click on the date and time turns the season's decorations off and on again; the gear menu's switch follows (and the other way round)
- **0.20.2** (2026-10-31, `b1ad088`) Gear menu: switches for the effects, each with what it does and what it's doing now: lettering mirrored in the glass edges (what was found in this picture), 3D shadows, animations (its fps result moved here from the notes), seasonal decorations on the date and time (today's season, how much of the next)
- **0.20.1** (2026-10-31, `ac0679e`) Buttons, pills, their icons and the volume slider cast the letters' shadow (down and left, tight dark + soft): more 3D, like HHC's glass buttons
- **0.20.0** (2026-10-31, `a6cad71`) Clock letters dressed for the season, cut to the letters: autumn leaves, winter snow caps, snowflakes and frost, spring blossoms, sprigs and grass, summer suns, palms, shells and waves; laid out anew each day, the next season creeping in over a season's last month; motif colors standing out from each line's

## 0.19 — Pilulka Kvalita, odrazy tlačítek na skle

- **0.19.8** (2026-10-31, `bc668db`) Glass: no thin dark rings in the middle (inside the rim each lens ring draws one flattened picture; layer by layer its soft edge let the shade under the cover through); edges mirror only lettering beyond them (text found once per picture in a thread: thin-stroke cells), elsewhere plain fisheye and magnifier as before
- **0.19.7** (2026-10-31, `2207188`) Glass clean: lens rings thinned to those moving the picture half a screen pixel or more (thousands of hairline rings in the flat middle piled up 8-bit rounding into concentric stripes; also about half the work), mirrored edge strips fade into the see-through part (no hard line)
- **0.19.6** (2026-10-31, `855bf0b`) Edge mirror fast again: the mirrored strips rendered once per lens build into textures, drawn only by the rings that see past the edges (each of thousands of rings at 4K drew the pictures twice more: menus stuttered)
- **0.19.5** (2026-10-31, `d3d7d47`) Edge mirror inside the lens instead of over it: what lies beyond the 'i' and gear water's top and bottom edges flipped where the rim bends it in, so it shows upside down there; the top buttons' and the KVALITA pill's reflections back as they were
- **0.19.4** (2026-10-31, `7038496`) Glass edges mirror what lies beyond them: the 'i' and gear water's top and bottom edges (and the KVALITA menu's) show it upside down, closest part at the edge, fading inwards; the top buttons and the pill shown only that way
- **0.19.3** (2026-10-31, `88d2e47`) KVALITA menu hangs from the tab, not the pill: open, it no longer widens the pill and halves the volume slider; a 10 px gap between the pill and its menu
- **0.19.2** (2026-10-31, `565c087`) Mirrors as KVALITA's was: buttons seen through the glass at the rim (bent like the rest), only upside down; the top buttons that way in the 'i' and gear water; the pill says the chosen stream (AAC 128 kbps), a gap before it
- **0.19.1** (2026-10-31, `70a6bc8`) Buttons mirrored upside down on the glass beside them: 'i', gear and X on the 'i' and gear water's top, the KVALITA pill on its menu's bottom (no longer upright at the rim)
- **0.19.0** (2026-10-31, `0bd3f8a`) Quality moves from the gear menu to a glass pill at the bottom right (like the 'i' panel's tabs), its list opening upwards with what plays now; the buttons sit as far from the bottom as from the left, the status above them only when it says something

## 0.18 — HRÁLO: všechny přehrané skladby

- **0.18.39** (2026-10-31, `b3efa2a`) Track lines and clock stretched taller to fill their room's height too (up to 2x)
- **0.18.38** (2026-10-31, `888ec3a`) Track lines flash again when the song changes or its lines update; picture notes: 'zaostřeno', the '% %' slip and 'for detail' fixed, older records say what wasn't measured then
- **0.18.37** (2026-10-31, `dabbf35`) Kept picture notes rewritten once in the new wording (CPU čekal na: …), and shown so from the other computer too
- **0.18.36** (2026-10-31, `99ef20e`) Enhancing steps: 'CPU čekal na: GPU (CPU ji jen zásobuje daty), rezervu pro plynulý obraz (0,2 s)'
- **0.18.35** (2026-10-31, `bfa31cb`) Automatické spuštění: at login as the tray icon only, with a switch to open in full screen instead
- **0.18.34** (2026-10-31, `bf92667`) Czech for texts not shown until needed: FLAC stream name, missing mpv
- **0.18.33** (2026-10-31, `9a82062`) Cache line in plainer Czech: na tomto počítači, na počítači …, dohromady
- **0.18.32** (2026-10-31, `81ba76d`) Remaining English texts in Czech: older picture notes (na CPU, za …), software Vulkan, status line, button tooltips
- **0.18.31** (2026-10-31, `d21c390`) Gear menu: 'Poslední vylepšený obrázek' keeps the last finished picture and its sizes (over restarts too) until the next one, with the one being enhanced below; older notes and leftovers in Czech
- **0.18.30** (2026-10-31, `3f353df`) Enhancing steps say what they waited for (animations, an open panel, the smoothness reserve, the GPU, one-thread picture saving); gear menu Czech more natural (Aktuální obrázek, testování, …)
- **0.18.29** (2026-10-31, `4033ce6`) Gear menu in Czech (rows, notes, enhancing steps, sharing, cache, updates); every enhancing step tells its watts and the CPU threads it kept busy; sharpening on all threads
- **0.18.28** (2026-10-31, `851714e`) HRÁLO song's cover on the main screen enhanced like the radio's (upscaled to the screen, from the shared cache when made before)
- **0.18.27** (2026-10-31, `4ce8bc4`) Czech names with their marks (Zapomenutý hříchy, Kabát) from the lookups, kept and shared for HRÁLO; month as ŘÍJEN; track info parts on their own lines without the dot
- **0.18.26** (2026-10-31, `c62726d`) Video stills squared at their own resolution, so the AI upscales them (blown up to 4096 by plain resizing they skipped it and looked blurry)
- **0.18.25** (2026-10-31, `8a7e828`) Song info in a wide window as fitted lines (each as wide as the room left of the cover, below the logo, above the buttons); clock lines in the month's colors, shuffled every day
- **0.18.24** (2026-10-31, `0a8c159`) AI upscaling: the tile size climbs back after a few good pictures (stuck at 32 px it kept the GPU a quarter busy and clocked down)
- **0.18.23** (2026-10-31, `b608e05`) Clock: weekday, day, month, year, time on lines of their own (day without the dot), below the top buttons; cache line counts the pictures here, on the other computer and in all
- **0.18.22** (2026-10-31, `4b4f6d0`) Clock above the volume slider: weekday, day and month, year, time (lowest), each as wide as the room right of the cover
- **0.18.21** (2026-10-31, `c1bd92e`) Track info and pictures shared between the computers; the background lookups split between them (each from its own end of the history, each song once)
- **0.18.20** (2026-10-31, `873aadf`) History filled in the background (pictures, track info: after every change to the lookups too); text the same size on every desktop (as Omarchy's 1.64x)
- **0.18.19** (2026-10-31, `2c51f1a`) Date above the volume slider, weekday and year stretched to fill; song facts with the full date and studio, city, state, country (all looked up again); date and clock no longer pump with the slider
- **0.18.18** (2026-10-31, `d9a2484`) Fix the history update's pattern (it stopped the app from starting)
- **0.18.17** (2026-10-31, `dd63af8`) History: old track info naming the release's country looked up again (on every computer)
- **0.18.16** (2026-10-31, `d3b4eef`) Song facts: no release country (the Beatles 'in Canada'); where it was recorded, else the artist's country
- **0.18.15** (2026-10-31, `56cef75`) Covers: a video's still squared (bars cut, middle square; tall ones with blurred sides, 4096 px); a year after the title no longer breaks the lookup; where it was recorded from Wikidata too
- **0.18.14** (2026-10-31, `0e60e49`) HRÁLO playback: studio versions first, a live one when there's nothing else
- **0.18.13** (2026-10-31, `b4cce60`) HRÁLO: opening a paused song's cover keeps it paused; 'i' tabs light only their pill
- **0.18.12** (2026-10-31, `c19408e`) Radio's buttons are the radio's; they act on a HRÁLO song only on its cover screen
- **0.18.11** (2026-10-31, `a773a4a`) HRÁLO: a paused song lets the radio be heard, and the radio's pause is the radio's again
- **0.18.10** (2026-10-31, `bda9590`) HRÁLO: a paused song's button in clear glass like the radio's stop
- **0.18.9** (2026-10-31, `0f07722`) HRÁLO: play buttons always red, with room to pump without the list's edge cutting them
- **0.18.8** (2026-10-31, `fc0945e`) HRÁLO songs on the main screen like the radio's (a click on the picture: its big cover, its lines, pause, stop and volume where they always are) with back left of 'i' (a tile like it), back again in HRÁLO where it was; the radio plays on muted meanwhile (live after; enhancing, history as ever); each row one glass play / pause like the radio's buttons, back to the radio resets it (every button play again); the radio's pause and stop act on the song while one plays. KDE: from full screen straight to the bottom-right corner (no stop at the top left), the tray icon and the taskbar always bring it up as a window in the corner, a second tray click minimises it
- **0.18.7** (2026-10-31, `42c503d`) HRÁLO: play / pause and stop on every song (and in the full-screen view): the song itself from YouTube, the studio version (YouTube Music's album audio, official audio or the artist's channel; never live, unplugged, cover, remix, another artist's song of that name), FLAC when there is one, else the best audio there is (opus ~128 kbps); the radio waits meanwhile and goes on live after; yt-dlp the system's or the official release fetched and checked, weekly. No more doubles: a song right after itself is one play (a long song named again after 10 minutes), the same song within 30 minutes too (the doubles so far removed); the history never deleted for room (a full disk: a play not kept here, the other computer has it); a picture trimmed for room fetched from the other computer when it's wanted again
- **0.18.6** (2026-10-31, `d1537ef`) 'i' panel tabs (DNES … HRÁLO): the same pill shape in glass like the pause, stop and gear buttons (magnifying what's behind, bent at the rim, lit like a dish), the chosen one red glass like pause
- **0.18.5** (2026-10-31, `b5c42a2`) HRÁLO: each song with its track info like the main screen (the album, its first release, where recorded when MusicBrainz answers, the show), in the full-screen view too; kept with the play and shared along; older plays get it looked up with their picture as they come into view
- **0.18.4** (2026-10-31, `9a74a46`) Panel tour: DNES, ZAJÍMAVOSTI, AKTUALITY, ZÍTRA, POZÍTŘÍ and HRÁLO 10 s each, each view a page further than the last tour showed it (its end before the top again), not the same top every time; song's first release when MusicBrainz can't be reached: the earliest year among Wikidata (Wikipedia's data), iTunes and Deezer (compilations and live versions left out, the exact day when one has it), not the matched album's date (a reissue: Nirvana '13 years ago')
- **0.18.3** (2026-10-31, `9a7048c`) Song pictures from YouTube when Deezer and iTunes don't have the song (Czech bands as Špinaví lůzři): a video of the song, its still (before any artist photo: the song itself is the surer match); else Deezer's artist photo (not its grey placeholder), else the artist's YouTube channel picture or a still of one of their videos; songs not found before looked up again once
- **0.18.2** (2026-10-31, `a560a48`) HRÁLO: a click on a song's picture shows it full screen (big, with the song, when, the album and show), the wheel goes to the newer / older song, a click or Esc closes it; songs found though named differently ('LOCOMOTION' = 'The Loco-Motion (Remastered 2002)': brackets, '- Remastered', a leading 'The', spaces and hyphens ignored, a letter or two off allowed), the artist's photo when the song is nowhere; songs not found before looked up again once
- **0.18.1** (2026-10-31, `11b36b4`) HRÁLO: each song with its album's picture, like the shows in DNES and ZÍTRA (kept with the play, shared along; older plays looked up one by one as they come into view, the logo meanwhile and when none is found)
- **0.18.0** (2026-10-31, `a8a28c6`) 'i' panel: HRÁLO, every song played (newest first, a heading for each day: the time, artist – song, album, show), more loaded as it scrolls, never-ending; every play kept (no limit), shared with the other computers both ways (the same song within 10 min is one play: each logs the stream itself, gaps filled from the other); a song logged again when the app restarted during it is kept once (and the doubles so far removed)

## 0.17 — Sdílení obrázků v místní síti

- **0.17.8** (2026-10-31, `d41c734`) Gear menu in the Beat Radio font (Oswald, titles semibold) and growing with the window like the rest (on a Full HD TV the standard sizes were too small to read); it opens at the top
- **0.17.7** (2026-10-31, `5c9bd42`) Glass buttons: no crosshair through the middle on the DeskMini (the lens rings' flattened picture keeps its alpha: opaque, GTK skipped what each ring seemed to cover, and the 780M left hairline gaps where a circle's quarters meet)
- **0.17.6** (2026-10-30, `9c4b555`) The panels' glass ('i', gear) shows the picture's echoes along its edges again: each ring drawn from every layer behind (built over the frames, ~3 s at 4K, plainly magnified meanwhile); the show card and buttons keep the flattened picture
- **0.17.5** (2026-10-30, `566c259`) Gear notes: 'Animations' measured on this screen (the last one's fps and slowest frame, the share of frames on time over 10 minutes); picture sharing also searches the networks of the house's routers on the way out (tracepath, 3 hops, private addresses: a computer behind a second router finds one on the outer network and does the fetching and handing over)
- **0.17.4** (2026-10-30, `94fbbf1`) Smooth animations (60 fps at 4K): the backdrop blurred once per picture at half size, a morph cross-fading two textures (a CSS blur redid it every frame: ~10 fps while morphing); the morph's softening blur at half resolution; lens glass from one flattened, opaque picture of what's behind (a ring of every layer behind cost ~1 ms, now 0.1), built over several frames within 8 ms a frame shared by all glass (the 'i' panel's 2800 rings took 2.1 s in one go: a frozen window, and past ~2 s the kernel reset the GPU, the app's drawing lost with it)
- **0.17.3** (2026-10-30, `8f9a2ad`) Enhancing: a picture another computer made already is fetched from it before making it ('fetched from Ren7000'); CPU denoising on 2 threads (16 were no faster: it waits on the memory, at 7x the CPU time); GPU steps note how busy the GPU was and its clock ('on GPU, 98 % busy at 1971 of 2000 MHz'), CPU steps the CPU's vector instructions (AVX2, AVX-512); the panel's automatic tour no longer pauses the enhancing (a touch does, as when opened by hand)
- **0.17.2** (2026-10-30, `96c8665`) Sharing opens its port in the firewall by itself (ufw or firewalld, local network addresses only; through pkexec, the administrator's password once if asked; the command shown when it can't); the mouse wheel scrolls the 'i' panel's list anywhere on it (over the pinned show and the tabs too), never the volume there
- **0.17.1** (2026-10-30, `3bfb050`) Picture notes: every step with its time, where it ran and the size it left ('quick AI redraw 4.3 s on GPU (4.9 MB)'), the original's size up front and the final size at the end (also for cached pictures)
- **0.17.0** (2026-10-30, `0908e41`) Picture cache shared on the local network: every computer with Rádio Beat keeps its own copy of all enhanced pictures (fetches what it lacks, hands over what others lack, port 48765, local addresses only, cache files only); pictures enhanced for at least 4K everywhere so the files are the same; gear switch 'Share pictures on this network'

## 0.16 — Hodiny a datum

- **0.16.10** (2026-10-30, `2b3c1d3`) Song recognition: a song recognised again in a later break shows again (it was skipped as shown already, even after the stream had named other songs); the gear note says why when a find isn't shown
- **0.16.9** (2026-10-30, `8ea8098`) Big pictures denoised on the GPU too (in overlapping strips: Renoir gives a picture over ~3 MP back black); the status says it's testing the GPU until it knows; sizes in KB / MB / GB as they come, the cache's used space too
- **0.16.8** (2026-10-30, `9833a74`) Clock reaches up to just below the volume slider's knob (a small gap); the time may take more of the width, the date lines more of the height
- **0.16.7** (2026-10-30, `efd1f6d`) Clock: the weekday as before, under it the day and month, then the year, each as big as fits beside the time
- **0.16.6** (2026-10-30, `2299a80`) A new show on air, and every half hour (:00, :30): the panel opens by itself, DNES for 20 s, then ZAJÍMAVOSTI, AKTUALITY, ZÍTRA and POZÍTŘÍ 10 s each, then closes (a touch stops it, leaving it open)
- **0.16.5** (2026-10-30, `7322429`) DNES: the coming shows right below the show on air (its gap measured without its margin)
- **0.16.4** (2026-10-30, `b7ddff1`) DNES by time: past shows greyed above, the show on air pinned one show from the top (the lineup scrolls behind it, hidden there), the coming ones below; the weekday as wide as its room; the clock's colon blinks
- **0.16.3** (2026-10-30, `dd3855b`) Clock: the time the room's full height at the right; left of it the weekday at the top, centred, a little bigger than the date below
- **0.16.2** (2026-10-30, `35277a1`) Button reflections as they were (a plain faint mirror, no bend round the corner)
- **0.16.1** (2026-10-30, `5c7d4aa`) The clock in the Beat Radio fonts, capitals: the time big on top, the date below, filling the room right of the cover below the slider (fitted to the letters' outlines)
- **0.16.0** (2026-10-30, `cef0d83`) Time and date in Czech at the bottom right (below the slider, right of the cover, as big as fits, against the screen's right edge)

## 0.15 — Panely ⓘ a ⚙ jako tekoucí voda

- **0.15.36** (2026-10-30, `b3821fd`) Started at login it opens in full screen (an update's restart with the window closed stays in the tray: --hidden)
- **0.15.35** (2026-10-30, `36086f2`) Panel tabs DNES first with equal gaps; views scroll by themselves after 15 s; a new show opens the panel for 10 s; stronger button reflections bent round the corner; even gaps between the dishes
- **0.15.34** (2026-10-30, `e6bae1b`) No contour lines along the water panels' rim (their lens rings a fifth of a pixel apart; it's drawn once and reused)
- **0.15.33** (2026-10-29, `94b343f`) Top tiles ('i', gear, close) one square size; the buttons reflected faintly on the water panels' glass, fading downwards (following their pumping)
- **0.15.32** (2026-10-29, `cd0453d`) Glass without artifacts: the lens bends over its outer three quarters, the middle one evenly magnified layer (inner rings had become thin strips whose seams showed as lines); rings half a pixel apart on all glass (no ring pattern on the small tiles); tiles and round buttons show the blurred bars under them as around them (a sharp cut made a hard edge in them); the 'i' tile square like the gear's, without pumping
- **0.15.31** (2026-10-29, `0ac01a0`) Pictures morph into each other again (a 4 s cross-fade, a little soft halfway), no crumbling
- **0.15.30** (2026-10-29, `31668a3`) Fluid panels: while the 'i' or gear water is open only the small pictures' quick steps run (quick AI pass, sharpened, not cached) and the full enhancing waits (a running one is stopped); with the panels closed, while the radio just plays, everything gets the full enhancing, the small pictures too
- **0.15.29** (2026-10-29, `64177a0`) Water panels' content flat again (only the glass behind it bends)
- **0.15.28** (2026-10-29, `b585612`) Enhancing steps die with the app even when stopped (SIGKILL as their parent-death signal: stopped ones ignored SIGTERM and were left behind holding GPU memory, seven of them); the 'i' and gear water open lighter (its content bent once it's open, coarser rings)
- **0.15.27** (2026-10-29, `b6d305e`) Glass bends round all the way in: the lens rings keep rounded corners (deeper than the corner radius they turned sharp and creased the picture diagonally)
- **0.15.26** (2026-10-29, `3ceef02`) Water panels: what's inside stays within the rectangle joining the corners' circle centers (the corner radius on every side, growing with it)
- **0.15.25** (2026-10-29, `0722cff`) What's inside the water panels bends with the glass too: true in the middle, squeezed and curving away towards the rounded edge (the tabs follow the rim, text scrolling to the bottom bends over it); each ring drawn in its own band only
- **0.15.24** (2026-10-29, `f20a219`) Water panels' rim refracts more (thicker glass); every small picture in the 'i' panel enhanced like the cover (AI-upscaled to twice its shown size when smaller, sharpened, denoised, cached; after the cover and the logo); picture changes and enhancing steps take 4 s
- **0.15.23** (2026-10-29, `da67f99`) Water panels: text and switches well inside the glass's rounded edge; the edge a thick rounded wall of glass (the bend gathered there, a wide shaded wall, lit edges and a reflection)
- **0.15.22** (2026-10-29, `7247d3e`) Curiosities never run dry: a sentence counts as shown only when it's on screen (it was marked when made, and the panel pulled items every frame: thousands marked unseen), one new item every 4 s at most; about what played in the last hour first, then 2, 4, 8 hours, a day, a week… the whole history, then the station's usual artists; English-only ones skipped while there's no translating; list lines (discographies) left out
- **0.15.21** (2026-10-29, `70889e6`) Water panels: what's under the glass sharp (the bars beside the cover too); the cover's outline bends round under the lens (its per-ring clip made angular steps)
- **0.15.20** (2026-10-29, `39135f0`) Water panels: the show card's full fish-eye again (no depth cap), the blurred background mirrored past the window's edges (no seam, no gray frame); the song info sized from where it really is (below the top bar, left of the cover; down to 55 %), words not broken; titles without reissue notes ('(Remastered 2015)'); actions app.info / app.settings (keys I, G)
- **0.15.19** (2026-10-29, `c14486a`) Glass near the window's edges looks thick (its curved edge bends the blurred background past the window too, not emptiness: that showed as a flat gray frame with a line inside)
- **0.15.18** (2026-10-29, `d259c95`) Water panels as thick as the show card: their curved edge as deep as the card's (not spread thin over the big panel), the card's corners
- **0.15.17** (2026-10-29, `960269f`) Water panels: exactly the show card's glass (as HHC's buttons have it), only bigger: its fish-eye, shading, edges and shadow, Background shade too; the panel-only tweaks taken out
- **0.15.16** (2026-10-29, `3c60835`) Song info size fixed: it shrinks again when too tall (that check had been lost) or too wide beside the cover, never below 85 % of its usual size, and grows to fill its place up to 1.6x
- **0.15.15** (2026-10-29, `2432bf2`) Song info as big as fits its place (beside the cover, below the logo, above the buttons): only its text is sized (the buttons stay), from its usual size up to 1.6x, never below 85 %; the 'i' tile square like the gear's and close's
- **0.15.14** (2026-10-29, `f9d51f0`) Song info never out over the cover: its lines break inside a long word too, and it shrinks when it's wider than beside the cover
- **0.15.13** (2026-10-29, `eba805c`) 'i' panel: the show on air leads today's lineup (DNES), not the curiosities
- **0.15.12** (2026-10-29, `f30e570`) Background shade slider wide (it fills its row's room, at least 220 px)
- **0.15.11** (2026-10-29, `60fda3a`) Where it was recorded: from the first releases (albums) too, when no recording says
- **0.15.10** (2026-10-29, `b9a53d6`) First-release line: a lone recording on a single release dated before the rest isn't taken as the first release (Paint It Black 1964); where it was recorded looked for in up to six recordings, the most released first
- **0.15.9** (2026-10-29, `18e1039`) Water panels thick: the fish-eye's bend gathered at the rounded rim (a curved wall of glass), the wall shaded wide, its top edge lit, a deeper shadow
- **0.15.8** (2026-10-29, `b64d914`) Water panels read as a lens again: a stronger fish-eye (1.35x, edges showing more of what's beyond), a curved reflection near the top left and a shaded rim
- **0.15.7** (2026-10-29, `fcd7f58`) Song info never up over the logo: it shrinks to fit below the top bar in full screen too, and starts below the top bar however big it is
- **0.15.6** (2026-10-29, `a977865`) Water panels clear, not matte (tint 8 %, a trace of blue); no ring lines on big glass (its lens steps half a pixel apart)
- **0.15.5** (2026-10-29, `99738b2`) 'i' panel: AKTUALITY, radiobeat.cz's news (the homepage's and the news page's, newest first): date, headline, picture and summary; a click opens the article
- **0.15.4** (2026-10-29, `fe1873c`) Water panels: the show card's glass as it was (fish-eye 1.25x over the whole glass, its edges, a shadow), even all over
- **0.15.3** (2026-10-29, `a311630`) Water panels are thick glass again (magnified 1.2x, bent hard along the rounded edge, a shaded side wall, a shadow below; the neck without one); the 'i' panel's lineup has each show's picture beside it again, the curiosities their artist's (or song's) picture from Wikipedia. Track details back: the first-release line is asked with the lookup's names (the stream's are misspelt: PAVLOVS ´DOG), titles without '(Remaster)', apostrophes matched (Pavlov's = PAVLOVS), else the album's release date; '&' in the recognition note no longer breaks it
- **0.15.2** (2026-10-29, `bde2102`) Both water menus reach from the cover's left edge to the screen's right one
- **0.15.1** (2026-10-29, `b0d4b44`) The gear's menu is water like the 'i' button's (the same Water: a neck squeezing from the button into a tall glass body, flowing down, drawn back up by a click outside; one open at a time). The neck is a water shape (the button's width, drawn in at the waist, spreading into the body, no edge of its own), the water faintly blue. Both sit beside the cover (from its edge to the screen's) and end above the volume slider, touching none; even glass (no darker bottom). Curiosities without a badge, just the name; tabs for curiosities / today / tomorrow / the day after (the wheel's button too), scrolling slower. Song info between the screen's left edge and the cover's. Covers change by eroding like old plaster: pieces come loose from a few spots and fall off the screen, the new picture behind. Each enhancing step: its time and the size it left, when new
- **0.15.0** (2026-10-29, `b186265`) 'i' glass button left of the gear: the programme and curiosities in glass water flowing down from it (a neck, then a tall body with a drop's front edge), drawn back up by a click outside; the wheel's button switches curiosities (scrolling slowly by themselves) → today → tomorrow → the day after; the wheel scrolls it (not the volume). The show card itself no longer sits beside the song. Covers change by peeling off piece by piece like a skin (to the enhanced version too). Each enhancing step keeps its time and gets the size it left, when new: sharpened in 0.3 s (2.3 MB)

## 0.14 — Rozpoznávání skladeb

- **0.14.6** (2026-10-29, `8602aee`) Picture now sizes: the fast enhanced one left out when there was no first pass
- **0.14.5** (2026-10-29, `d650bab`) Gear menu: 'Recognise songs' right below 'Picture now'
- **0.14.4** (2026-10-29, `724b57a`) Picture now sizes: source / fast enhanced (the first step shown) / final enhanced
- **0.14.3** (2026-10-29, `07dec9d`) Picture now: a line with the sizes (original / enhanced), and the row sits right below 'Volume on the sound card'
- **0.14.2** (2026-10-29, `dcee9f2`) Picture now: the original's and the enhanced picture's size in MB, and the cache: GB used of the GB set aside. First-release line: where it came out and how long ago (Poprvé vydáno 8. listopadu 1971 v USA (před 55 lety) · Nahráno v …), worked out when shown
- **0.14.1** (2026-10-29, `997adf3`) Song recognition reports in the gear menu (its switch and the notes at the bottom) what it did: found … / nothing found in … s, the clip's recording time, the service
- **0.14.0** (2026-10-29, `cf6921b`) Recognise songs (testing, gear switch, off by default): while the stream's title is just the station (jingles, breaks), a 12 s clip is recognised once a minute through Shazam's unofficial API (shazamio, installed once into ~/.local/share/radio-beat/shazam); a song found is shown and remembered like a named one

## 0.13 — Karta pořadu: zajímavosti z Wikipedie

- **0.13.7** (2026-10-29, `cafec2d`) Show card mix: seed artists Rádio Beat plays fill in until the history has 15 of its own (the stream sends no song names at night, the mix had only the station's 15 sentences and ran dry); no songs yet: artists instead; the show's title isn't stored as a host
- **0.13.6** (2026-10-29, `8feca90`) Show card mix: 10 % the station (RÁDIO BEAT badge, no headline beside it), 90 % artists, songs (their articles, only when they name the artist) and now and then hosts; no sentence shown again within a month (remembered across restarts in shown.json)
- **0.13.5** (2026-10-29, `a06ffa9`) Station texts in the show card keep the ON AIR badge (not RÁDIO BEAT)
- **0.13.4** (2026-10-29, `86be8b2`) Show card mix from whole Wikipedia articles (often 30-200 sentences per artist), made ahead in the background, each source's sentences in a shuffled order: about 180 different texts an hour instead of the same few; sentences split the Czech way (1. května 1997, 60. let, tzv. aren't sentence ends: 'začalo vysílat 1.' was cut off); English-only articles translated a sentence at a time and remembered (skipped once MyMemory's free daily limit is used up)
- **0.13.3** (2026-10-29, `50ab3da`) History: the station's own jingles aren't recorded as plays
- **0.13.2** (2026-10-29, `d001b57`) Show card timing: every minute the programme for 8 s, between a new text every 15 s (the last one to the minute; the song's own up to the minute), typed faster when long. History in SQLite (~/.local/share/radio-beat/history.db, newest first, 50000 plays kept): every song (when, artist, title, album, show, host) and every host seen with their shows; the mix draws on artists played before a restart and on all the station's hosts (NAŠE HLASY)
- **0.13.1** (2026-10-29, `32b2943`) Show card's second turn is a mix that's never the same for long: the song playing once, then about the artists played lately (other sentences each time), about 15 % who's on air, 5 % the station itself, each with another lead-in (Víte, že…, Zajímavost:, Mimochodem:, U mikrofonu: …) and badge
- **0.13.0** (2026-10-29, `8503963`) Show card takes turns with the song: half the time NOW PLAYING with a few sentences about it (Wikipedia, Czech first, else translated through MyMemory), else 'Did you know?' about the artist or who's on air, typed out letter by letter at a fast typist's pace, scrolling when longer than 5 lines; all in Czech, in ostravština by default (short vowels, fest, fajne; gear switch for standard Czech), the first-release line too. A copy run from the source folder never updates itself (a test copy had replaced the installed app); the enhancer's processes end with the app and when it quits (stopped ones were left behind)

## 0.12 — NPU (testování), mezipaměť obrázků

- **0.12.10** (2026-10-29, `924e667`) Updates looked for right at start (installed and restarted if new), then every 5 minutes
- **0.12.9** (2026-10-29, `1bfbf70`) A right click anywhere in the window mutes, and again lets it play (like the speaker button)
- **0.12.8** (2026-10-29, `fe3032d`) A click on the speaker mutes (crossed-out icon, tooltip 'Sound on'), again lets it play; the volume stays as set; with the sound card's volume it mutes the card; a restarted mpv stays muted
- **0.12.7** (2026-10-29, `596448d`) Full picture cache: the least used pictures go first (times shown from the cache, counted in uses.json), of those the one not shown for the longest
- **0.12.6** (2026-10-29, `1807d85`) Picture cache limited by space, not count: up to 80 % of the space it could have on this computer's disk (free + its own), never leaving under 2 GB free; oldest pictures go first
- **0.12.5** (2026-10-29, `02f295e`) Enhanced covers and logos come from the cache at once (checked when the picture is asked for, before the GPU test that every start runs, e.g. after an update); the logo is asked for at start; 800 kept instead of 300
- **0.12.4** (2026-10-29, `8aa17c7`) The gear stops pumping when its menu opens
- **0.12.3** (2026-10-29, `c884523`) The gear pumps under the mouse like the other buttons (only on the gear itself, not while in its menu), still ticking round
- **0.12.2** (2026-10-29, `4a94c9f`) Volume slider pumps a little sideways too (1.5 %), its knob pumps like the buttons
- **0.12.1** (2026-10-29, `cddb67b`) Narrow window: the show card spans the window's width, so its wrapped text is measured for the width it gets and the lines no longer overlap
- **0.12.0** (2026-10-28, `b40a344`) NPU for AI upscaling (testing, gear switch 'Use the NPU (testing)', off by default): checks the amdxdna driver, ONNX Runtime with AMD's Vitis AI provider and an NPU-built model in ~/.local/share/radio-beat/npu, says what's there and what's missing; with all three, the AI step runs on the NPU first (npu_upscale.py, tiled), else on the GPU as before. The show card pumps under the mouse as a whole (outline, text and all, 4 %) in step with its breathing lens

## 0.11 — Řádek o skladbě: první vydání, kde nahráno

- **0.11.2** (2026-10-28, `ee2c022`) Under the gear menu the show card's edge and shadow are blurred too (no sharp line left); pictures always go into Real-ESRGAN as plain 8-bit RGB (it couldn't read some covers: 'decode image failed', they stayed small)
- **0.11.1** (2026-10-28, `657edd8`) Volume slider pumps up and down only (1.8x tall, not sideways into the speaker), its knob along (1.35x); the show card breathes under the mouse: its magnification rises and falls once a second like a magnifier lifted and lowered (coarser lens steps while it moves, smooth)
- **0.11.0** (2026-10-28, `07e8f29`) Song info: a line with the song's first release date and where it was recorded (MusicBrainz: official releases, asked again for anything older until nothing is, the oldest plain recording's studio; at most one request a second, remembered per song). Smooth animations: the show card's lens drawn from a texture until something behind it changes (not a hundred layers a frame); the picture enhancing waits while anything animates (cover morph, flag, flash, the mouse on a pumping or ticking control) and takes at most 92 % of the time otherwise

## 0.10 — Kvalita jako skleněný seznam

- **0.10.1** (2026-10-28, `d43595e`) Gear and close in glass tiles like the dishes (close in red glass, it pumps under the mouse); the gear's ticking turn is its own icon only: the quality arrow and the ticks in its menu no longer spin (they did while the mouse was in the menu), the arrow just bobs
- **0.10.0** (2026-10-28, `a8805a3`) Quality: own glass list (best sound first, a note on each stream's codec) opening from the menu's left edge, a bobbing arrow (never turns); show card corners r 28 with straight sides (not shrunk by fit); the volume slider pumps under the mouse, follows it pixel by pixel and the volume glides there softly (saved once dragging stops)

## 0.9 — Sklo: lupa, rybí oko, skleněná tlačítka

- **0.9.10** (2026-10-28, `9bb8cd4`) Glass: the cover's own edges stay straight in it (the lens bends what's inside them, not the outline): the cover's side no longer looks like a round end of the show card
- **0.9.9** (2026-10-28, `9c93198`) The logo at the top left waves like a flag in the wind under the mouse (held at its left edge, a wave running to the free edge, folds shaded), and settles flat again
- **0.9.8** (2026-10-28, `062892e`) Glass is a fish-eye everywhere (show card, gear menu, pause/stop/speaker): magnified most in the middle (1.25x, dishes 1.3x), less and less towards the edges, which show a little of what lies beyond; under the mouse pause, stop, the speaker and close pump once a second and the gear ticks round like a watch hand twice a second
- **0.9.7** (2026-10-28, `1f1c8fc`) Under the gear menu the whole show card is out of focus (glass, edges and text); pause, stop and the speaker magnify sharply (1.18x, the cover sharp in them even over the bars)
- **0.9.6** (2026-10-28, `6b20d85`) Under the gear menu the show card's text is out of focus (blurred), so the menu's own text reads clearly; the card's glass and picture stay sharp
- **0.9.5** (2026-10-28, `561efb6`) Pause, stop and the speaker in round dishes of glass (concave: what's behind looks smaller, more towards the rim; shade along the top inside edge, light along the bottom), pause in red glass; sizes 68 / 56 / 46, the speaker icon bigger
- **0.9.4** (2026-10-28, `aa0da05`) Gear menu glass lies over the whole window: the cover, the text and the show card's glass show through it, magnified and bent (the window drawn once per redraw into a texture at the screen's scale)
- **0.9.3** (2026-10-28, `9d35766`) KDE: back to the bottom-right corner (on the taskbar by the clock), also each time it leaves full screen or comes back from minimised; opens in full screen (gear switch 'Start in full screen', on by default)
- **0.9.2** (2026-10-28, `e662a7f`) Glass: thick and see-through. Wider curved side wall (rim 1.6 radii), magnified middle 1.12x, shaded wall, light on the top and bottom edges, a shadow below; inside it the window's own shading (no gray tint or milky sheen: black stays black); the gear menu sees the cover sharp. Fit to the window no longer flips between two sizes (it kept relaying out)
- **0.9.1** (2026-10-28, `4c62d99`) Gear menu is glass like the show card (drawn from where its popup sits over the window, darker tint for its small text); all text gets a shadow at its left and bottom edges, readable on pale covers
- **0.9.0** (2026-10-28, `973ed03`) Glass: a magnifying glass (middle magnified 1.07x, the rim bent the other way, a cubic curve between), over the blurred bars too, so it reads as thick glass with rounded sides

## 0.8 — Horní lišta: logo, ozubené kolo, zavřít

- **0.8.1** (2026-10-28, `e5c1258`) Glass: the blurred bars sit where the window shows them (the backdrop's CSS zoom was counted twice); top bar not faded when the window isn't focused; fit to the window measured once the new sizes are in (it went back and forth before)
- **0.8.0** (2026-10-28, `6a87293`) Top bar: station logo (sharp, enhanced) at the left, gear and a red close at the right, always above the text; no minimise/full-screen buttons (double click switches). Volume: Rádio Beat's own again by default, mouse wheel anywhere. Tray click minimises on KDE when the window is up; KDE opens it bottom-left. Updates: looked for every 5 minutes, installed and restarted by itself (switch). Glass: the blurred bars beside a square cover stay blurred in it (not bent), rounder corners. Text, card and buttons shrink to fit a window's height

## 0.7 — Hlasitost zvukové karty

- **0.7.2** (2026-10-28, `3b716f2`) Fixes: black covers (GPU denoise returns all black for big pictures, 2160 px on Renoir, without an error: checked, redone on the CPU and that size kept off the GPU; black cached pictures are thrown away); the updater only goes forwards (it reinstalled an older GitHub version over a newer one); shorter sound-card row text
- **0.7.1** (2026-10-28, `5d341a4`) Gear menu opens over the window and is sized to fit inside it (narrower in a narrow window, scrolls when taller): a popup with no room below the gear was closed at once on KDE (window in the corner, low on the screen) and the gear then looked dead
- **0.7.0** (2026-10-28, `748e399`) Volume slider = the sound card's volume (new default, gear-menu switch): a volume knob on the card or the desktop's volume moves the slider, the slider turns the card; mpv stays at 100 %. Through pactl (subscribe + JSON lists), mpv's stream named radio-beat to find its card

## 0.6 — AI zvětšování obrázků (Real-ESRGAN)

- **0.6.9** (2026-10-28, `495b6a5`) On-air card: 55 % of a wide window, as tall as the song's lines, all its text shown (wrapped, no cut titles), room inside the glass; sharp lens refraction (hard 1 px layers, no blending); the song's lines and the card flash briefly when the song changes
- **0.6.8** (2026-10-28, `9ed3179`) On-air card beside the song in a wide window (above it in a narrow one), only as wide as its text; sharper glass refraction (narrow rim, strong bend); all-black AI pictures are caught again (the check was fooled by the alpha channel), cache v5
- **0.6.7** (2026-10-28, `23fde6c`) On-air card at the right, only as wide as it needs (less of the cover hidden); its glass refracts what's behind it like a lens (true in the middle, bent towards the rim) instead of blurring it
- **0.6.6** (2026-10-28, `cfb84b4`) Pictures always go through AI: they wait for the GPU test instead of a plain resize, a failed AI pass retries with smaller tiles, a non-AI result isn't cached; quick AI pass first (realesr-animevideov3), then x4plus for covers and x4plus-anime for the logo; software Vulkan (CPU) when there's no GPU; fixed a race that dropped jobs at start
- **0.6.5** (2026-10-28, `19239d6`) Every picture fills the window or screen (AspectFrame: small ones are stretched too); the last cover stays up until the next one is found (no logo flash between songs); stronger sharpening (fine + wide unsharp, CAS); pictures big enough already are AI-redrawn for detail (Real-ESRGAN from half size, back to their size), big ones are upscaled from half size
- **0.6.4** (2026-10-28, `bdc04b2`) Pictures big enough for the screen are still sharpened and denoised (at their own size); the gear menu says no upscaling was needed
- **0.6.3** (2026-10-28, `888f474`) Pictures enhanced in steps, each shown when done: AI upscaling (Real-ESRGAN on any Vulkan GPU, the dedicated one first, never a software one; smooth Lanczos without a GPU), then sharpening (CAS), then denoising (NL-means on the GPU through ffmpeg's Vulkan when it can, else the CPU) and sharpening again; ffmpeg downloaded when the system has none. Gear menu: 'Enhance pictures' switch with what's available, and 'Picture now' with source size → screen size and each step's time
- **0.6.2** (2026-10-28, `eedee24`) AI upscaling keeps itself working: checks its files at start (downloads again if missing or damaged, retries while offline), GPU self-test with smaller tiles if it times out (remembered), one retry per picture, damaged cache cleared; status in the settings menu
- **0.6.1** (2026-10-28, `fda1fbd`) AI upscaling from 1.05x stretch (was 1.25x)
- **0.6.0** (2026-10-28, `85ec266`) AI upscaling: covers (and the logo) that full screen would stretch are redrawn by Real-ESRGAN on the GPU (Vulkan), installed by itself (47 MB, checksum-checked), cached; plain scaling until it's ready or without Vulkan

## 0.5 — Celý obal přes rozmazané pozadí

- **0.5.0** (2026-10-28, `8bbe8a1`) Cover shown whole and as big as it fits, over a blurred copy filling the rest of the window or screen (no cropping, any aspect ratio)

## 0.4 — Vlastní MPRIS přehrávač

- **0.4.2** (2026-10-27, `d5e22ff`) Text, show card, picture and buttons scale with the window: full screen fills the screen; a bit bigger in the window too
- **0.4.1** (2026-10-27, `e853bc1`) Double click toggles full screen (not on buttons, the slider or the title strip)
- **0.4.0** (2026-10-27, `3836e97`) Own MPRIS player (Rádio Beat, song, cover) instead of mpv's plugin; covers 1200 px (Deezer max) / 1920 px (iTunes)

## 0.3 — Karta pořadu (ON AIR) a celá obrazovka

- **0.3.3** (2026-10-27, `c02510b`) Settings menu placed inside the window from the gear's real position (gap from edges, a bit lower); app icon top left as big as the header buttons
- **0.3.2** (2026-10-27, `cb2d5f4`) Settings menu capped at 320 px (scrolled box): rows wrap instead of widening it; 'Quality' label
- **0.3.1** (2026-10-27, `b053996`) Settings menu: no wider than the app; update note wrapped under the Updates row, opens leftwards from the gear
- **0.3.0** (2026-10-27, `0e3c0c2`) On air card (show now + next from radiobeat.cz) and full screen toggle (button, F11, Esc)

## 0.2 — Aktualizace z GitHubu, KDE: okno u hodin

- **0.2.4** (2026-10-25, `075d517`) KDE: open in the bottom-right corner on the taskbar (KWin script) instead of under the mouse
- **0.2.3** (2026-10-25, `f378481`) get.sh installs the commit the updater saw
- **0.2.2** (2026-10-25, `3738eb2`) Update: fetch get.sh of the newest commit itself (master's raw file is cached)
- **0.2.1** (2026-10-25, `9b43b3c`) Update check by commit (install records it), not by file contents (GitHub's file cache lags)
- **0.2.0** (2026-10-25, `5584016`) Settings: update from GitHub; KDE: open next to the tray (KWin rule)

## 0.1 — Přehrávač: GTK4 + mpv, skladba s albem a obalem, volba kvality

- **0.1.7** (2026-10-25, `49cc271`) Installer: refresh KDE's app cache so the window gets the Rádio Beat icon
- **0.1.6** (2026-10-25, `29e00cd`) Flathub mpv: share the socket folder as xdg-run/radio-beat (Flatpak refuses plain /run paths)
- **0.1.5** (2026-10-25, `92df004`) Installer: add Flathub to the user's remotes before installing mpv (Bazzite has it system-wide only)
- **0.1.4** (2026-10-25, `d6ee1e5`) README, one-command install (get.sh), license
- **0.1.3** (2026-10-25, `25d2257`) Restarts playback on its own: reconnects when the stream ends or stalls, moves to a backup server, restarts mpv; Flathub mpv for Bazzite
- **0.1.2** (2026-10-25, `283982f`) Cover fills the window with title and controls over it; tray icon; pause / stop quits; plays on start
- **0.1.1** (2026-10-25, `7da16f0`) mpv exits with the app even when it's killed
- **0.1.0** (2026-10-25, `953bab5`) Rádio Beat player: GTK4/libadwaita + mpv, now playing with album and cover, quality choice, start at login
