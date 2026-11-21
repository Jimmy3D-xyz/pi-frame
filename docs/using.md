# Using the frame

[← Back to the README](../README.md)

Open `http://piframe.local/` on a phone or computer on the same Wi-Fi (or
scan the QR code the frame shows when it starts).

- [Photos and albums](#photos-and-albums)
- [Videos](#videos)
- [The slideshow](#the-slideshow)
- [On-screen info](#on-screen-info)
- [Demo mode](#demo-mode)
- [Seeing what's on the frame](#seeing-whats-on-the-frame)
- [Turning the screen on and off](#turning-the-screen-on-and-off)
- [Portrait mounting](#portrait-mounting)
- [Wi-Fi and the setup screens](#wi-fi-and-the-setup-screens)
- [Page password](#page-password)
- [Home Assistant and the image server](#home-assistant-and-the-image-server)

## Photos and albums

Drag photos onto the Photos tab, or pick them from your phone. iPhone HEIC
photos work. Each photo is resized for the screen when it's added, keeping
the date and place it was taken. Photos go into albums; choose which albums
are in the slideshow.

To keep an album in step with a cloud folder or a NAS, see
[Synced folders](synced-folders.md).

**Fit or fill** (Display tab): fit shows the whole photo with a soft blurred
backdrop (or black or white) around it; fill crops it to fill the screen.

## Videos

Upload videos the same way as photos. Big files go up in 8 MB pieces, so a
dropped connection just retries; if the page is closed, choose the same file
again to carry on where it stopped (unfinished uploads are kept for a week).
If uploads are slow, plug the Pi into the router with a cable, or shrink the
video first.

Each video is checked when it arrives:

- **H.264 up to 1080p** is stored as it is and is ready straight away.
- **Everything else** (iPhone HEVC, HDR or Dolby Vision, 4K, 60 fps) is
  converted in the background to 1080p, 30 fps H.264, with HDR toned down so
  it doesn't look washed out. The Photos tab shows progress. Expect a few
  minutes per 30 seconds of 4K phone video; it runs at low priority so the
  slideshow carries on. To skip the wait, export clips as 1080p H.264 (e.g.
  HandBrake's "Fast 1080p30"), or set the iPhone to Settings → Camera →
  Formats → Most Compatible.

1080p is the most the Pi 4's video decoder handles smoothly; videos are
scaled up to fill bigger screens. Videos copied onto the card by hand aren't
converted, so use compatible files for those.

When a video comes up, it plays once (or for the time limit under Display →
Videos), then the slideshow moves on. Scheduled changes never cut a video
short; the Next button does. Tap a video in the Photos tab to play it now.

**Sound** is off by default. When on, it goes to the HDMI port the monitor
is plugged into. Each video's loudness is measured when it's added and
evened out on playback, so quiet and loud videos come out at a similar
volume (the file isn't changed). If everything is still too quiet with the
monitor turned up, add Volume boost (+6 or +12 dB); a limiter stops loud
moments distorting.

## The slideshow

**Schedule tab**: when the photo changes, using the same rules as the ESP32
firmware (`minute hour day-of-week`, e.g. every 30 minutes), in random order
or in turn.

**Slideshow styles** (Schedule tab): tick one, or several to take turns.

- **Classic**: one photo at a time. When a photo's shape doesn't suit the
  screen (a portrait photo on a landscape screen), two are shown side by side.
- **Story**: a collage of 3, 4 or 5 photos (or a mix), then each of those
  photos full screen in turn, then a new collage.
- **Spotlight**: collage, one of its photos, back to the collage, the next
  photo, and so on.
- **Living collage**: a collage where one photo at a time is swapped for
  another, sliding in like a print laid on a table. After every space has
  changed twice, a new layout. "Swap a photo every" sets the pace.

With several ticked, the frame changes style every 30 minutes to a day
(Take turns every), always at the end of a round: after a story, or once a
living collage's layout has finished, never half-way through. The web
page says which one is showing.

Collage and single-photo times are set separately (e.g. 5 and 10 minutes).
Collages put portrait photos in tall spaces and suit both screen shapes.
Story, Spotlight and Living collage use photos stored on the frame.

**Photos around videos** (on by default): when a video doesn't fill the
screen, such as a landscape video on a portrait screen or a phone video on
a landscape screen, photos that suit the empty spaces go around it.

**Video moments**: every 15 minutes to 4 hours, a short clip (10 seconds to
1 minute) from a random point in a random video, then back to whatever was
showing.

**Changing pictures** (Display tab):

- **Fades, zooms, slides and 3D turns** (the default): in Story and
  Spotlight a photo grows out of its collage space to fill the screen, and in
  Spotlight shrinks back into it; Living collage photos slide in; and every
  other change between two photos is a 3D turn (a card flip, the photo
  tipping away, or a turning cube), the rest fades.
- **Fades, zooms and slides**: no 3D turns.
- **Fades only**, or **Instant**.

Speed sets the fade length (Normal is 1.5 seconds); moves follow it. All of
these run at 60 fps on the display hardware (see
[Screen and hardware](screen-and-hardware.md#fades-moves-and-colour)).

## On-screen info

Display → On-screen info adds small frosted-glass panels in the corner you
choose, in small, medium or large:

- **Time and date**, 12- or 24-hour.
- **Weather** for a place you search for: temperature, conditions and
  today's high and low, from Open-Meteo (free, no account), refreshed every
  15 minutes.
- **Photo details**: when and where the photo was taken, read from the photo
  itself, with the place name from OpenStreetMap. On a photo's anniversary it
  says "2 years ago today".
- **Jokes and fun facts**: a joke, a fact or both, changing with each
  picture, from a built-in family-friendly collection that works offline. Add
  your own lines too.

The panels move by a few pixels each hour so nothing sits on the same
pixels for ever, and they're hidden while a video plays.

## Demo mode

Like a TV in a shop: sample photos, collages, and short slides about what
the frame can do, some with a QR code for its page. It shows off every kind
of change: 3D turns between photos, a photo shrinking into a collage, photos
swapped into it, and one growing out again (unless Changing pictures is set
to Instant). Each sample is tagged
"Sample photo" with its title and artist, in the corner opposite the
on-screen info, and slides change every 10–20 seconds.

- A set-up frame with **no photos yet** shows it by itself, and switches to
  your photos as soon as the first one arrives.
- **Display → Starting up → Demo mode** shows it any time, even with photos
  on the frame (for showing the frame off). Switch it off again to go back.

The samples are 8 public-domain paintings from the
[Art Institute of Chicago](https://www.artic.edu/open-access/open-access-images)
(Monet, Renoir, Kandinsky and others, CC0) and 8 NASA photographs (Earth
from the space station, Saturn, the Webb telescope's Cosmic Cliffs). Each
one's title, creator, source and licence is in `photoframe/data/demo/credits.json`.
They live with the software, not in your albums: they're never in your
photo count, history or backups.

## Seeing what's on the frame

- **Tap the preview** on the web page to see it large, exactly as on the
  screen (on-screen info included), with a link to the full-size picture.
- **Live view** (`http://piframe.local/live`) mirrors the screen in a
  browser, following each change within a couple of seconds. Its controls
  hide when idle and it keeps a tablet awake, so an old tablet can be a
  second frame.

## Turning the screen on and off

- **Screen schedule** (Display tab): on and off times, e.g. on at 07:00 and
  off at 22:30.
- **Presence sensor**: on when someone's in the room, off when they've left.
- **Screen button** on the web page, or the [physical button](screen-and-hardware.md#physical-buttons).

With a monitor that supports DDC/CI, the monitor itself can go into standby
too, and dim at night. See [Monitor brightness](screen-and-hardware.md#monitor-brightness-and-standby).

## Portrait mounting

Choose Portrait under Display. The layout assumes the screen was turned
anticlockwise from landscape; if photos appear upside down, choose "Upside
down (180°)". The boot screen turns to match.

## Wi-Fi and the setup screens

**Welcome**: each time the frame starts, it shows a QR code for its web
page for 30 seconds (Display → Starting up; 1–2 minutes, or off), with its
IP address and network once Wi-Fi connects. The Next button skips it.

**No photos**: with nothing to show, the frame shows a QR code for its web
page. The first photo you add replaces it straight away.

**Wi-Fi setup**: if the frame isn't online within 2 minutes of starting
(15 seconds when it has no Wi-Fi network saved, as on a new frame), or loses
Wi-Fi for more than 5 minutes, it starts its own hotspot called
`PiFrame-XXXX` and shows two QR codes:

1. Join the frame's Wi-Fi (the password is on the screen and changes each boot).
2. Open the setup page, `http://10.42.0.1/setup`. Most phones open it by
   themselves as a "sign in to network" page.

Pick your network and enter its password. While the hotspot is up, the
frame also retries your saved Wi-Fi every 3 minutes, so a router outage
fixes itself once the router is back.

**Link check**: being connected isn't the same as working. The frame pings
the router every 15 seconds; if it stops answering, the frame re-joins,
preferring another radio of the same network (e.g. the router's 5 GHz one).
An internet outage alone doesn't trigger this.

To change network from the web page: Photo source → Change Wi-Fi network.

## Page password

Maintenance → Page password protects the web page, and is needed before
software updates can be installed from it. It's separate from the SSH
password you set in Raspberry Pi Imager.

## Home Assistant and the image server

**Home Assistant**: add the ESP32 PhotoFrame integration with the host
`piframe.local`. Next photo, current image, settings and notifications work;
battery and sensor entities show as unavailable, as the Pi has none.

**Image server**: instead of its own photos, the frame can show pictures
from an [esp32-photoframe-server](https://github.com/aitjcize/esp32-photoframe-server)
or any "Auto Rotate URL". Add a device in the server's dashboard with the
host `piframe.local`; it sends its address to the frame. The stock server
dithers photos for e-paper, so they look grainy on a monitor: the patch in
`server-patch/` adds full colour (see [For developers](developers.md#the-image-server-patch)).
