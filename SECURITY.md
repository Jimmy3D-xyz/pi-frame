# Security

Pi Frame runs on people's home networks and can update itself, so security
problems matter. Thank you for reporting them privately.

## Reporting a problem

Please **don't open a public issue**. Instead use GitHub's private
reporting: the repository's **Security** tab → **Report a vulnerability**.
Only the maintainer sees it.

Include what you found, how to reproduce it, and which version
(Maintenance shows it, or `photoframe/__init__.py`). You'll get an answer
as soon as possible; fixes go into a new release, and frames set to install
updates automatically pick them up overnight.

## Which versions

Only the newest release is supported. Frames update from the Maintenance
tab, or by themselves.

## What's in scope

The frame's web page and API, the update and backup paths, the installers
(`deploy/`, `kit/`) and the root helpers. The frame is meant for a home
network: putting its page on the open internet isn't supported (use a VPN
such as Tailscale; see `docs/using.md`). How the page is protected is
described in `docs/developers.md` → Security.
