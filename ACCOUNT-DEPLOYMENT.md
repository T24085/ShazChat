# ShazChat accounts rollout

## Rollout order

1. Give players the v1.3.0 installer first. They need this version to create or sign in to an account.
2. When they are ready, stop the current server and run `start-server-novatec-casa.bat` from this project folder. The new server requires account sign-in before a player can join a team.
3. The first successful sign-in creates `server-data/accounts.json`. Keep that file private and backed up; it contains salted password and recovery-PIN hashes, never readable passwords.

## Reset a password

On the computer hosting the server, run `reset-player-password.bat`. Enter the registered player name and the new password twice. The player can sign in immediately with the new password; restarting the server is not required.

## Player self-service

Signed-in players can use **Settings → Account** to change their own password or permanently delete their account. Both actions require the current password. A password change signs out the account on other devices. Deleting an account immediately releases its room role and signs it out everywhere.

New players also choose a 4–12 digit recovery PIN during account creation. From the sign-in screen, **Reset password** lets them set a new password using their player name and recovery PIN. Accounts created before this feature do not have a recovery PIN; use the host reset script for those accounts, or have the player create a new account.

## Account rules

- Player names: 3–32 letters, numbers, periods, dashes, or underscores.
- Passwords: 8–128 characters.
- Names are case-insensitive for sign-in, but their original capitalization is shown in the app.
