"""Official ShazChat update channel configuration.

The release signing public key is intentionally compiled into every client.
Only the matching private key, stored outside this repository, can publish an
update that clients will accept.
"""

APP_VERSION = "1.11.3"
UPDATE_MANIFEST_URL = "https://downloads.novatec.casa/capper-times/stable.json"
UPDATE_DOWNLOAD_HOST = "downloads.novatec.casa"

# Generated for the ShazChat stable channel.  This is public by design.
# Never put the matching private key in this repository or in an R2 bucket.
UPDATE_PUBLIC_KEY_B64 = "a87FYBlQQhVrj8OUObV07t3vzqcly9Dyc/7akscOJBo="
