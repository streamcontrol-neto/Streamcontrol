#!/bin/bash
# ══════════════════════════════════════════════════════
# StreamControl — סקריפט התקנה אוטומטי
# הרץ פקודה אחת — הכל קורה אוטומטית
# ══════════════════════════════════════════════════════

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO="https://raw.githubusercontent.com/YOUR_USERNAME/streamcontrol/main"
INSTALL_DIR="$HOME/streamcontrol"
FIREBASE_URL="https://YOUR_PROJECT.firebaseio.com"
FIREBASE_SECRET="YOUR_FIREBASE_SECRET"
R2_ACCOUNT_ID="YOUR_R2_ACCOUNT_ID"
R2_BUCKET="streamcontrol-music"
R2_ACCESS_KEY="YOUR_R2_ACCESS_KEY"
R2_SECRET_KEY="YOUR_R2_SECRET_KEY"
R2_PUBLIC_URL="https://YOUR_PUBLIC_URL.r2.dev"
VERSION="1.0.0"

echo ""
echo "${BLUE}╔════════════════════════════════════════╗${NC}"
echo "${BLUE}║     StreamControl — התקנה אוטומטית    ║${NC}"
echo "${BLUE}║              גרסה ${VERSION}                  ║${NC}"
echo "${BLUE}╚════════════════════════════════════════╝${NC}"
echo ""

# ── שלב 1: עדכון חבילות ──────────────────────────────
echo "${YELLOW}[1/6] מעדכן חבילות...${NC}"
pkg update -y -q && pkg upgrade -y -q 2>/dev/null || true

# ── שלב 2: התקנת תלויות ──────────────────────────────
echo "${YELLOW}[2/6] מתקין Python, MPV...${NC}"
pkg install -y python mpv curl git 2>/dev/null || {
  echo "${RED}שגיאה בהתקנת חבילות. בדוק חיבור אינטרנט.${NC}"
  exit 1
}
pip install --quiet requests schedule boto3 2>/dev/null || pip install requests schedule boto3

# ── שלב 3: יצירת תיקייה ──────────────────────────────
echo "${YELLOW}[3/6] יוצר תיקיית התקנה...${NC}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# ── שלב 4: הורדת קבצים ───────────────────────────────
echo "${YELLOW}[4/6] מוריד קבצי המערכת...${NC}"
curl -sSL "$REPO/streamer.py"          -o streamer.py
curl -sSL "$REPO/client_display.html"  -o client_display.html

# ── שלב 5: יצירת config ייחודי ───────────────────────
echo "${YELLOW}[5/6] יוצר הגדרות ייחודיות...${NC}"

# מספר סידורי אוטומטי מכתובת MAC
MAC=$(ip link show 2>/dev/null | grep 'link/ether' | head -1 | awk '{print $2}' | tr -d ':' | head -c 8 | tr '[:lower:]' '[:upper:]')
DEVICE_ID="SN-${MAC:-$(date +%s | tail -c 6)}"
HOSTNAME=$(hostname 2>/dev/null || echo "streamer")

cat > config.json << EOF
{
  "device_id":       "${DEVICE_ID}",
  "device_name":     "${HOSTNAME}",
  "device_location": "ממתין להגדרה",
  "firebase_url":    "${FIREBASE_URL}",
  "firebase_secret": "${FIREBASE_SECRET}",
  "r2_account_id":   "${R2_ACCOUNT_ID}",
  "r2_bucket":       "${R2_BUCKET}",
  "r2_access_key":   "${R2_ACCESS_KEY}",
  "r2_secret_key":   "${R2_SECRET_KEY}",
  "r2_public_url":   "${R2_PUBLIC_URL}",
  "volume":          70,
  "auto_start":      true,
  "version":         "${VERSION}",
  "allowed_genres":  [],
  "active_genres":   []
}
EOF

# ── שלב 6: הפעלה אוטומטית ────────────────────────────
echo "${YELLOW}[6/6] מגדיר הפעלה אוטומטית...${NC}"

# הוסף ל-bashrc
AUTOSTART="cd $INSTALL_DIR && python streamer.py >> streamer.log 2>&1 &"
grep -qF "streamer.py" "$HOME/.bashrc" 2>/dev/null || echo "$AUTOSTART" >> "$HOME/.bashrc"

# Chrome Kiosk (אם קיים)
CHROME_CMD="chromium-browser --kiosk --noerrdialogs --disable-infobars --no-first-run $INSTALL_DIR/client_display.html"
grep -qF "client_display" "$HOME/.bashrc" 2>/dev/null || echo "$CHROME_CMD &" >> "$HOME/.bashrc"

# ── התחל עכשיו ────────────────────────────────────────
echo ""
echo "${GREEN}╔════════════════════════════════════════╗${NC}"
echo "${GREEN}║         ✅ התקנה הושלמה בהצלחה!       ║${NC}"
echo "${GREEN}╚════════════════════════════════════════╝${NC}"
echo ""
echo "${BLUE}מספר סידורי: ${DEVICE_ID}${NC}"
echo "${BLUE}שלח מספר זה למנהל המערכת${NC}"
echo ""
echo "${YELLOW}מתחיל את הנגן...${NC}"
python "$INSTALL_DIR/streamer.py" &
echo ""
echo "${GREEN}✅ הנגן פועל! המסך יתעדכן תוך שניות.${NC}"
echo ""
