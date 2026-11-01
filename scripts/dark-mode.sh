#!/bin/sh
# Waybar module for the system light/dark mode (the setting browsers and GTK apps follow).
# No argument: print the current mode as JSON, then again on every change.
# "toggle": switch between dark and light.
SCHEMA=org.gnome.desktop.interface
KEY=color-scheme

show() {
    case "$1" in
        *prefer-dark*) printf '{"alt": "dark", "class": "dark", "tooltip": "Dark mode (click for light)"}\n' ;;
        *)             printf '{"alt": "light", "class": "light", "tooltip": "Light mode (click for dark)"}\n' ;;
    esac
}

if [ "$1" = toggle ]; then
    case "$(gsettings get $SCHEMA $KEY)" in
        *prefer-dark*) gsettings set $SCHEMA $KEY prefer-light ;;
        *)             gsettings set $SCHEMA $KEY prefer-dark ;;
    esac
    exit 0
fi

show "$(gsettings get $SCHEMA $KEY)"
# stdbuf: without it gsettings holds its output back when writing to a pipe
stdbuf -oL gsettings monitor $SCHEMA $KEY | while read -r line; do show "$line"; done
