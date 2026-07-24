ShazChat - Distribution Package
===============================

QUICK START
1. Run ShazChat-Setup.exe to install on a Windows computer, or double-click ShazChat.bat for a portable run.
2. The first time you connect, choose Create a new account, then enter your player name, password, and a 4–12 digit recovery PIN. Sign in with it on later launches.
3. Choose Capper 1, Capper 2, Offense, or Defense, then choose a team.
4. Press V to start or cycle the timer as a capper.
5. Settings apply automatically as you make changes; there is no Apply button.
6. Settings are organized into compact Settings, Timers, Roles / Team, and Account tabs.
   Map presets live with the timer controls.

TEAM PRIVACY
- You can switch roles in Settings.
- Only people in your selected team can see its roster and capper times.
- The first person in a team can optionally set a password from Settings.
- That password disappears automatically when the team becomes empty.

CHAT
- The lower-left ShazChat panel stays on top of the game.
- Use the title-bar minimize button if you want to hide the full chat panel on a
  one-monitor setup; the lightweight click-through message overlay stays available.
- Drag any edge or corner of the full chat panel to resize it. It starts compact
  but can be made larger for easier reading.
- In Settings > Chat, choose your message font size and color. Player names stay
  white and team chat labels stay green so callouts remain instantly recognizable.
- Switch between Global and My Team. Global messages reach all connected players;
  My Team messages are visible only to the people in your selected team.
- Severe slurs are blocked by the server before they can be sent, saved, or shown
  to other players. Normal gameplay banter is still allowed.
- Press Enter or Send to post a message.
- Timer hotkeys automatically pause while you type in chat or a settings field,
  then immediately return when you click back into the game. Your timer and chat
  keybind letters can be typed normally in the chat box.
- Starting a timer never brings ShazChat to the foreground or takes focus
  away from the game.
- Timer hotkeys work while movement or other gameplay keys are held down.
- In Settings > Timers, click a hotkey field and press the key you want. Letter,
  number, numpad, F1–F24, and mouse-button bindings are supported.
- Press Enter to open the in-game chat input. Change that key in Settings > Timers
  if you prefer another key; Send or Escape returns focus to the game.
- The transparent chat overlay automatically expands for wrapped messages, so
  longer player names and messages are fully visible.
- Timer and chat overlays start on by default. They stay off only if you check
  Settings > Turn off gameplay overlays and global hotkeys (compatibility mode).

TEAM ACTIVITY
- Settings shows which team channels are active, the player count, and the first
  player name. It does not reveal another team's roster, chat, or capper times.

CONNECTION
The app connects to wss://capper.novatec.casa automatically. Internet access is
required for team synchronization. If the app cannot reach the server, it still
runs as a local timer.

ACCOUNT HELP
- Your password is stored on the ShazChat server only as a salted hash; the
  server owner cannot see it.
- If you forget it, contact the server owner. They can reset it from the server
  host, then you sign in with the new password.
- You can also choose Reset password on the sign-in screen and use your player
  name plus recovery PIN to set a new password yourself.
- While signed in, use Settings > Account to change your password or permanently
  delete your account. Both actions require your current password. Deleting an
  account also releases any team role you are holding.

UPDATES
- ShazChat checks the official update file when it opens. It does not send
  your name, team, device ID, or gameplay data.
- When an update is available, choose Download and install. The installer is
  verified before it runs. You can also use Settings > Check for updates.

TROUBLESHOOTING
- If the overlay or V hotkey is blocked, run ShazChat.bat as Administrator.
- If Windows Defender warns about the file, use the trusted release supplied by
  your team lead.
- Linux players should clone the repository and run ShazChat.sh. Global hotkeys
  work on X11/XWayland; pure Wayland desktops may block them by design.
