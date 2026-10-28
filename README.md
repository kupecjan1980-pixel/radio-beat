# Rádio Beat player (unofficial)

A small Linux desktop player for the Czech station [Rádio Beat](https://www.radiobeat.cz/).
Not made by or affiliated with the station; it plays the public streams.

- Album cover fills the window, with song, artist and album over it (looked up on Deezer, then iTunes; 1200–1920 px, sharp on a Full HD screen)
- On air: the show playing now (picture, name, host, what it's about) and the next one, from radiobeat.cz's programme
- Full screen: the button next to settings, a double click on the picture, or F11 (Esc leaves)
- Plays as soon as it starts; Pause, and Stop (quits)
- Restarts playback on its own: reconnects when the stream ends or stalls for 20 s,
  moves to a backup server after 3 failed tries, restarts mpv if it crashes
- Tray icon: click shows / hides the window, middle or right click pauses, scroll changes the volume;
  closing the window keeps it playing
- Audio quality: AAC 128, MP3 128 (two servers), AAC 64; FLAC is listed automatically if the station ever offers one
- Start at login (plays with only the tray icon)
- KDE Plasma: opens in the bottom-right corner, flush with the right edge and sitting on the taskbar
  (a small KWin script the app loads; switch in settings)
- Updates from settings: Check compares with GitHub, Update installs the new version and restarts the app
- Media keys and the desktop's player widget (MPRIS): shows as Rádio Beat with the song and cover; mpv itself stays hidden

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/kupecjan1980-pixel/radio-beat/master/get.sh | bash
```

Run it again to update; `./install.sh --remove` (from a checkout) removes it.
Everything goes into your home folder (`~/.local`); no sudo.

Needs Python 3 with GTK 4 and libadwaita (python-gobject) and mpv. Without a system mpv
(Bazzite and other atomic systems) the installer adds mpv from Flathub for you.
The station's logo is downloaded from radiobeat.cz when installing, for the app icon.
