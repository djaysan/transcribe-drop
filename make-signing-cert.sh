#!/bin/sh
# make-signing-cert.sh — one-time: create a local code-signing certificate for install.sh.
# Without one, every rebuild of Transcribe Drop.app gets a new ad-hoc signature, macOS sees
# a different app, and the Microphone + Screen & System Audio Recording permissions have to
# be given again. Signed with this certificate, rebuilds keep them.
# It's self-signed and lives only in this Mac's login keychain: it isn't an Apple Developer
# ID, so a copy sent to someone else still opens with right-click → Open as before.
set -e
NAME="Transcribe Drop Local Signing"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
BUNDLE_ID="com.lverbeeck.transcribe-drop"

if security find-identity -v -p codesigning | grep -q "\"$NAME\""; then
  echo "'$NAME' is already set up. Run ./install.sh to build the app with it."
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF

echo "==> Creating the certificate (valid 10 years)…"
# /usr/bin/openssl (LibreSSL) on purpose: Homebrew's OpenSSL 3 encrypts .p12 files in a
# format the macOS keychain can't import.
/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$TMP/cert.cnf" \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
/usr/bin/openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -out "$TMP/cert.p12" -passout pass:td

echo "==> Adding it to your login keychain…"
# -T: codesign may use the key without asking on every build.
security import "$TMP/cert.p12" -k "$KEYCHAIN" -P td -T /usr/bin/codesign >/dev/null

echo "==> Trusting it for code signing (macOS asks for your login password)…"
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$TMP/cert.pem"

# The next build is signed differently from the ad-hoc one macOS knows, so clear the old
# permissions once: the next recording asks again, and from then on the answer sticks.
tccutil reset ScreenCapture "$BUNDLE_ID" >/dev/null 2>&1 || true
tccutil reset Microphone "$BUNDLE_ID" >/dev/null 2>&1 || true

echo ""
echo "Done. Now run ./install.sh: it signs the app with '$NAME' from now on."
echo "If macOS asks whether codesign may use the key, click Always Allow."
