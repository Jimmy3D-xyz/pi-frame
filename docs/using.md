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
- [Admin options and passwords](#admin-options-and-passwords)
- [Away from home](#away-from-home)
- [When the frame needs attention](#when-the-frame-needs-attention)
- [Security updates](#security-updates)
- [Home Assistant and the image server](#home-assistant-and-the-image-server)

## Photos and albums

Drag photos onto the Photos tab, or pick them from your phone. iPhone HEIC
photos work. Each photo is resized for the screen when it's added, keeping
the date and place it was taken. Photos go into albums; choose which albums
are in the slideshow.

To keep an album in step with a cloud folder or a NAS, see
[Synced folders](synced-folders.md).

**Fit or fill** (Slideshow tab): fit shows the whole photo with a soft blurred
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

When a video comes up, it plays once (or for the time limit under Slideshow →
Videos), then the slideshow moves on. Scheduled changes never cut a video
short; the Next button does. Tap a video in the Photos tab to play it now.

**The Video button** (next to Next on the web page) plays one of the
frame's videos now. If a collage is on screen, the video plays in one of its
spaces, the one that best suits its shape, with the rest of the collage
around it, and the collage comes back when the video ends. A video close to
the space's shape fills it (a little is cropped, like the photos); a very
different shape (a phone video in a wide space) is shown whole, with the
dark mat beside it. With no collage on screen, the video plays full screen.
While it plays, Next becomes **Stop video**.

**The Clip button** plays a short part of a video, from a random point,
like a video moment (its length is set under Slideshow → Video moments, 20
seconds to start with). In a collage, when one clip ends the next starts in
another space, from a different video when there is one, until every space
has had a clip; then the collage carries on. **Stop video** ends the run.
With no collage on screen, it plays one clip full screen.

After any video in a collage style (Story, Spotlight or Living collage),
the frame goes back to the picture that was on screen before it.

**Sound** is off by default. When on, it goes to the HDMI port the monitor
is plugged into. Each video's loudness is measured when it's added and
evened out on playback, so quiet and loud videos come out at a similar
volume (the file isn't changed). If everything is still too quiet with the
monitor turned up, add Volume boost (+6 or +12 dB); a limiter stops loud
moments distorting.

## The slideshow

**Slideshow tab → Changing photos**: when the photo changes, using the same rules as the ESP32
firmware (`minute hour day-of-week`, e.g. every 30 minutes), in random order
or in turn.

**Slideshow styles** (Slideshow tab): tick one, or several to take turns.
Tap a style's name (not its tick box) to open its settings.

- **Classic**: one photo at a time. When a photo's shape doesn't suit the
  screen (a portrait photo on a landscape screen), two are shown side by
  side (switch this off in Classic's settings).
- **Story**: a collage of 3, 4 or 5 photos (or a mix), then each of those
  photos full screen in turn, then a new collage.
- **Spotlight**: collage, one of its photos, back to the collage, the next
  photo, and so on.
- **Living collage**: a collage where one photo at a time is swapped for
  another, sliding in like a print laid on a table. After every space has
  changed twice, a new layout. "Swap a photo every" sets the pace. With 3D
  turns on, every other swap turns over in 3D in its own space instead of
  sliding in. After
  every few swaps, one of its photos grows out of its spot to fill the
  screen for a while, then shrinks back (this can be switched off).

With several ticked, the frame changes style after every round, or every
30 minutes to a day (Take turns), always at the end of a round, never
half-way through one. A round is one story, one Spotlight collage and its
photos, a living collage's layout (until every spot has changed twice), or
5 Classic photos. The web page says which style is showing.

Story and Spotlight share their collage and photo times (e.g. 5 and 10
minutes); Living collage has its own swap time (5 minutes to start with).
Collages put portrait photos in tall spaces and suit both screen shapes.
Story, Spotlight and Living collage use photos stored on the frame.

**Next** moves on one step: the next photo, collage or swap. **New set**
(next to it on the web page, or hold the Next button for a second) skips
the rest of the round: a new collage and its photos, a new living collage
layout, or the next style when several take turns. A new set either
fades in or slides in over the old one (from a different side each time). **3D** does the same as New set (with
Classic, the next photo), but with a 3D turn (card flip, tip away and cube
in turn), drawn there and then: it plays after a few seconds (longer at
slower speeds).

**Photos around videos** (on by default): when a video doesn't fill the
screen, such as a landscape video on a portrait screen or a phone video on
a landscape screen, photos that suit the empty spaces go around it.

**Video moments**: every 15 minutes to 4 hours, a short clip (10 seconds to
1 minute) from a random point in a random video, so the videos come up even
among lots of photos. With a collage on screen it plays in one of the
collage's spaces, with the rest of the collage around it; otherwise full
screen. Then the slideshow carries on from where it was.

**Transitions** (Slideshow tab):

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

Screen → On-screen info adds small frosted-glass panels in the corner you
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
- **Maintenance → This frame → Demo mode** (an admin option) shows it any time, even with photos
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

- **Screen schedule** (Screen tab): on and off times, e.g. on at 07:00 and
  off at 22:30.
- **Presence sensor**: on when someone's in the room, off when they've left.
- **Screen button** at the top of the web page (it shows whether the screen is on; tap it to switch), or the [physical button](screen-and-hardware.md#physical-buttons).

Beside it, a dot shows whether the page can reach the frame, and which Wi-Fi network the frame is on (amber when it's away, on its own Wi-Fi; red if the page can't reach it).

With a monitor that supports DDC/CI, the monitor itself can go into standby
too, and dim at night. See [Monitor brightness](screen-and-hardware.md#monitor-brightness-and-standby).

## Portrait mounting

Choose Portrait under Maintenance → Frame hardware (an admin option). The layout assumes the screen was turned
anticlockwise from landscape; if photos appear upside down, choose "Upside
down (180°)". The boot screen turns to match.

## Wi-Fi and the setup screens

**Welcome**: each time the frame starts, it shows a QR code for its web
page for 30 seconds (Screen → Starting up; 1–2 minutes, or off), with its
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

**Away from home** (taking the frame to show someone, or a router outage):
a frame that's been set up shows the setup screen for a minute (press
Next to skip it), then carries on with its photos, as they're all stored on
the frame. Its own Wi-Fi stays on, and a small panel in a corner shows how
to join it (scan its QR code, or use the name and password shown). Then
open `http://10.42.0.1/` for the full web page: browse, change styles, even
add photos from that phone. Or join the place's Wi-Fi from the setup page.
While away, the clock and date are hidden until the frame has the right
time (the Pi has no clock battery, so it only knows the time once it's
online), weather is hidden, and the frame tries your saved Wi-Fi every 10
minutes (its own Wi-Fi drops for about a minute each time). Back home, it
rejoins your Wi-Fi by itself and the panel goes.

Maintenance → Wi-Fi and network lists the saved networks, so you can
forget one you used away from home.

To change network from the web page: Maintenance → Wi-Fi and network →
Change Wi-Fi network (with the admin options unlocked).

## Admin options and passwords

The page has two levels, so other people at home can use it without being
able to change the important things.

**Everyday use** works for anyone who can open the page: the Photos,
Slideshow and Screen tabs, choosing where photos come from, and reading the
health information in Maintenance.

**Admin options** are locked with the **admin password**, chosen in the
welcome wizard: software updates, backups, Wi-Fi settings, synced folders,
the image server's address and token, SSH, restarting, resetting the frame,
the passwords, and setting the frame up (Maintenance → This frame: name,
time zone, Home Assistant address, demo mode; Frame hardware: orientation,
hardware fades, buttons, presence sensor; Screen resolution). To use
them, open Maintenance and enter the admin password at the top. They lock
again after 15 minutes without being used, when you close the browser, or
with **Lock now**. After a few wrong passwords the page makes you wait a
little before trying again (longer each time).

Frames set up before version 0.33.0 use their page password as the admin
password to start with. Change either under Maintenance → Passwords. A frame
with no password at all asks you to set an admin password at the top of
Maintenance.

**Page password** (optional, Maintenance → Passwords): your browser asks for
it before showing the page at all (the username can be anything). Use one
if people you don't want changing the photos can join your Wi-Fi. If you use
the Home Assistant integration or the image server, enter it in their frame
settings too. Pick a different one from the admin password if others will
know it.

Both are separate from the SSH password.

Forgotten the admin password? See [Troubleshooting](troubleshooting.md).

## Away from home

Don't open the frame to the internet by forwarding a port on your router:
the page isn't made for that (no HTTPS, so passwords could be read on the
way). To reach it away from home, put the frame and your phone on
[Tailscale](https://tailscale.com/), a free private network: the page then
opens from anywhere, and nobody else can see it. The frame accepts its
Tailscale name (`piframe.your-tailnet.ts.net`) as well as `piframe.local`
and its IP address.

The frame only answers to its own names. If you reach it some other way
(a name of your own on your router, say), add that name to
`PHOTOFRAME_ALLOWED_HOSTS` (see [For developers](developers.md#settings-for-the-service)).

## When the frame needs attention

If something has needed a person for an hour and won't sort itself out, a
small panel appears on the screen: **Pi Frame needs attention · Open
piframe.local → Maintenance**. It doesn't say what's wrong, so visitors see
nothing private. It's for:

- an automatic backup or a software update that keeps failing,
- a synced folder whose sign-in has run out,
- the SD card filling up or showing errors, or the power supply being too
  weak right now,
- security updates not installing for a few days, or a restart for them
  waiting for days.

Wi-Fi problems aren't on this list: the frame has its own screens for those.

The top of Maintenance says why the panel is showing. The panel goes away by
itself once the problem is fixed. To hide it before then, unlock the admin
options and choose **Hide until something new** (a different problem shows
it again). To never show it, untick **Show a notice on the screen…** under
Maintenance → This frame.

## Security updates

Each night at about 03:40 the frame installs Raspberry Pi OS's **security**
updates: fixes for the parts that open photos and videos, SSH, encryption
and the rest of the system. Only Debian's security fixes are installed;
feature updates, and the kernel and firmware from Raspberry Pi's own
archive, are left alone so the frame doesn't change under you.

When something was installed, Pi Frame restarts (a few seconds, in the
night). When an update needs the whole Pi to restart (a core part like the
system library or OpenSSL), the frame does that while the screen is
scheduled off; with no screen schedule, between about 03:50 and 04:30.

Maintenance shows when updates last installed. If they keep failing (for
example, no internet for days) or a restart has waited for days, the
on-screen notice appears. See
[Troubleshooting](troubleshooting.md#security-updates) to run them now or
read their log.

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
