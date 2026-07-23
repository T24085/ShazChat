# ShazChat update releases

## One-time Cloudflare setup

1. In Cloudflare R2, create a bucket for official releases, such as `capper-times-releases`.
2. Connect the custom domain `downloads.novatec.casa` to that bucket. It must serve public HTTPS files.
3. Never store the private signing key in R2, Google Drive, GitHub, or this repository.
4. On the release computer, run `python tools/create_update_signing_key.py` once. Copy the printed public value into `updater_config.py` as `UPDATE_PUBLIC_KEY_B64`, then build version 1.2.0. The private key stays in ShazChat's local application-data folder.

## Publish a release

1. Set the new version in `updater_config.py` and `installer/ShazChatInstaller.iss`.
2. Build `release/ShazChat.exe` and `release/ShazChat-Setup.exe`.
3. Create a manifest, for example:

   ```powershell
   python tools/publish_release.py --version 1.2.0 --installer release/ShazChat-Setup.exe --notes "Server-backed player accounts and password resets."
   ```

4. Upload the installer first to:
   `capper-times/releases/1.2.0/ShazChat-Setup.exe`
5. Open that HTTPS URL in a private browser window and confirm it downloads.
6. Upload `release/stable.json` last to `capper-times/stable.json`. Configure its cache lifetime very low (or purge it) so clients receive the new release promptly.
7. Open `https://downloads.novatec.casa/capper-times/stable.json` and confirm it is valid JSON.

The manifest is the only mutable stable-channel file. Versioned installers remain immutable so prior versions are available for manual recovery.

## Recovery

- If an update is bad, publish a higher fixed version; clients never silently downgrade.
- If an installed update must be reversed immediately, have the affected player manually run the prior versioned installer from R2.
- If the signing key is lost or exposed, stop publishing, create a new key, change the embedded public key in a new manually distributed installer, then resume releases on the new channel.

## Privacy

The app only requests `stable.json` over HTTPS. It does not attach a player name, team, device identifier, installed version, or gameplay data. Cloudflare will still have ordinary HTTPS access logs for the request.
