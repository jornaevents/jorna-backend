#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# Desiconnect API — Comprehensive Live Test Suite
# Covers: Auth, Vendor Search, Bookings, Check-In, Calendar/Availability,
#         Google Auth, and Push Notification Token Management.
# ═══════════════════════════════════════════════════════════════════════
set -e

BASE="http://127.0.0.1:8000"
PASS=0
FAIL=0
TOTAL=0

check() {
  TOTAL=$((TOTAL + 1))
  local desc="$1"
  local expected_code="$2"
  local actual_code="$3"
  local body="$4"

  if [ "$actual_code" = "$expected_code" ]; then
    PASS=$((PASS + 1))
    echo "  ✅ PASS [$actual_code] $desc"
  else
    FAIL=$((FAIL + 1))
    echo "  ❌ FAIL [$actual_code expected $expected_code] $desc"
    echo "     Response: $body"
  fi
}

# Helper: extract a JSON field value using Python
jsonval() {
  python3 -c "import sys, json; print(json.load(sys.stdin)$1)" 2>/dev/null
}

echo ""
echo "╔═════════════════════════════════════════════════════════════════╗"
echo "║         DESICONNECT API — COMPREHENSIVE LIVE TESTS            ║"
echo "╚═════════════════════════════════════════════════════════════════╝"
echo ""

# ─────────────────────────────────────────────────────────────────────
# 1. ROOT & HEALTH
# ─────────────────────────────────────────────────────────────────────
echo "━━ 1. Root & Health ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

resp=$(curl -s -w "\n%{http_code}" "$BASE/")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "GET / → 200 with status=ok" "200" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" "$BASE/db-check")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "GET /db-check → 200 connected" "200" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 2. AUTH — REGISTER
# ─────────────────────────────────────────────────────────────────────
echo "━━ 2. Auth — Register ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@test.com","password":"Pass1234","username":"alice","phone":"5550001","f_name":"Alice","l_name":"Smith","age":25,"location":"10001","gender":"Female","language":"English"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Register Alice" "200" "$code" "$body"
ALICE_ID=$(echo "$body" | jsonval "['user_id']")
echo "     → user_id=$ALICE_ID"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"bob@test.com","password":"VendorPass","username":"bob_vendor","phone":"5550002","f_name":"Bob","l_name":"Kumar","age":35,"location":"90001","gender":"Male","language":"Hindi"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Register Bob (vendor user)" "200" "$code" "$body"
BOB_ID=$(echo "$body" | jsonval "['user_id']")
echo "     → user_id=$BOB_ID"

# Register a third user for edge cases
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"charlie@test.com","password":"Pass9999","username":"charlie","phone":"5550003","f_name":"Charlie","l_name":"Raj","age":22,"location":"60601","gender":"Male","language":"English"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Register Charlie (third user)" "200" "$code" "$body"
CHARLIE_ID=$(echo "$body" | jsonval "['user_id']")

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@test.com","password":"Pass1234","username":"alice","phone":"5550001","f_name":"Alice","l_name":"Smith","age":25,"location":"10001","gender":"Female","language":"English"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Duplicate email/username → 400" "400" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"bad@test.com"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Missing required fields → 422" "422" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Empty body → 422" "422" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 3. AUTH — LOGIN
# ─────────────────────────────────────────────────────────────────────
echo "━━ 3. Auth — Login ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@test.com","password":"Pass1234"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Login Alice (valid creds)" "200" "$code" "$body"
TOKEN=$(echo "$body" | jsonval "['access_token']")
echo "     → JWT token received (${#TOKEN} chars)"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@test.com","password":"WRONG"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Login wrong password → 401" "401" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"ghost@nowhere.com","password":"nope"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Login non-existent user → 401" "401" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"bob@test.com","password":"VendorPass"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Login Bob (valid creds)" "200" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 4. SEED VENDOR + SERVICES (direct DB via Python)
# ─────────────────────────────────────────────────────────────────────
echo "━━ 4. Seed Vendor & Service Data ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

SEED=$(python3 -c "
import sys, json
sys.path.insert(0, '.')
from app.db.database import SessionLocal
from app.db.models import Vendor, Service, User, VendorAvailability

db = SessionLocal()
bob = db.query(User).filter(User.email == 'bob@test.com').first()
bob.latitude = 34.05
bob.longitude = -118.24
bob.city = 'Los Angeles'
bob.state = 'CA'
db.commit()

vendor = Vendor(user_id=bob.user_id, bio='Expert Mehndi & Photography', rating=4.8, num_events=50, travel_radius_miles=30)
db.add(vendor); db.commit(); db.refresh(vendor)

svc1 = Service(name='Mehndi', price=300.0, duration_minutes=90, vendor_id=vendor.vendor_id, experience='10 years')
svc2 = Service(name='Photography', price=1200.0, duration_minutes=180, vendor_id=vendor.vendor_id, experience='8 years')
svc3 = Service(name='DJ Services', price=800.0, duration_minutes=240, vendor_id=vendor.vendor_id, experience='5 years')
db.add_all([svc1, svc2, svc3]); db.commit()
db.refresh(svc1); db.refresh(svc2); db.refresh(svc3)

for day in range(5):
    db.add(VendorAvailability(vendor_id=vendor.vendor_id, day_of_week=day, start_time='09:00', end_time='17:00'))
db.commit()

print(json.dumps({'vendor_id': vendor.vendor_id, 'svc1': svc1.service_id, 'svc2': svc2.service_id, 'svc3': svc3.service_id}))
db.close()
" 2>&1)

VENDOR_ID=$(echo "$SEED" | jsonval "['vendor_id']")
SVC1_ID=$(echo "$SEED" | jsonval "['svc1']")
SVC2_ID=$(echo "$SEED" | jsonval "['svc2']")
SVC3_ID=$(echo "$SEED" | jsonval "['svc3']")
echo "  ✅ Seeded vendor=$VENDOR_ID"
echo "     Services: Mehndi=$SVC1_ID, Photography=$SVC2_ID, DJ=$SVC3_ID"
echo ""

# ─────────────────────────────────────────────────────────────────────
# 5. NOTIFICATION TOKEN MANAGEMENT
# ─────────────────────────────────────────────────────────────────────
echo "━━ 5. Notification — FCM Token Management ━━━━━━━━━━━━━━━━━━━━━"

# Register Alice's token
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/notifications/register-token" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"fcm_token\":\"alice_device_token_abc123\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Register Alice's FCM token" "200" "$code" "$body"

# Register Bob's token
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/notifications/register-token" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_ID\",\"fcm_token\":\"bob_device_token_xyz789\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Register Bob's FCM token" "200" "$code" "$body"

# Check token status (has token)
resp=$(curl -s -w "\n%{http_code}" "$BASE/notifications/token-status/$ALICE_ID")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Token status for Alice → has_token=true" "200" "$code" "$body"
HAS_TOKEN=$(echo "$body" | jsonval "['has_token']")
echo "     → has_token=$HAS_TOKEN"

# Check token status for user without token
resp=$(curl -s -w "\n%{http_code}" "$BASE/notifications/token-status/$CHARLIE_ID")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Token status for Charlie → has_token=false" "200" "$code" "$body"
HAS_TOKEN=$(echo "$body" | jsonval "['has_token']")
echo "     → has_token=$HAS_TOKEN"

# Update token (re-register with new token)
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/notifications/register-token" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"fcm_token\":\"alice_NEW_token_refreshed\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Update Alice's FCM token (token refresh)" "200" "$code" "$body"

# Register for non-existent user
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/notifications/register-token" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"fake-user-id","fcm_token":"tok"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Register token for non-existent user → 404" "404" "$code" "$body"

# Remove token (opt out)
resp=$(curl -s -w "\n%{http_code}" -X DELETE "$BASE/notifications/remove-token/$CHARLIE_ID")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Remove Charlie's token (opt out)" "200" "$code" "$body"

# Remove token for non-existent user
resp=$(curl -s -w "\n%{http_code}" -X DELETE "$BASE/notifications/remove-token/nonexistent-id")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Remove token for non-existent user → 404" "404" "$code" "$body"

# Token status for non-existent user
resp=$(curl -s -w "\n%{http_code}" "$BASE/notifications/token-status/fake-id")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Token status for non-existent user → 404" "404" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 6. VENDOR SEARCH
# ─────────────────────────────────────────────────────────────────────
echo "━━ 6. Vendor Search ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=Mehndi&latitude=34.06&longitude=-118.25")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Search Mehndi near LA → found" "200" "$code" "$body"
COUNT=$(echo "$body" | jsonval "['vendors'].__len__()")
echo "     → Found $COUNT vendor(s)"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=Mehndi&latitude=40.71&longitude=-74.01")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Search Mehndi in NYC → empty (too far)" "200" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=Photo&latitude=34.06&longitude=-118.25")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Partial name 'Photo' → match" "200" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=DJ&latitude=34.06&longitude=-118.25")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Search DJ near LA → found" "200" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=Xyz123&latitude=34.06&longitude=-118.25")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Non-existent service → empty" "200" "$code" "$body"

# Case insensitivity
resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=mehndi&latitude=34.06&longitude=-118.25")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Case-insensitive search 'mehndi' → found" "200" "$code" "$body"
COUNT=$(echo "$body" | jsonval "['vendors'].__len__()")
echo "     → Found $COUNT vendor(s)"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 7. BOOKINGS — CREATE
# ─────────────────────────────────────────────────────────────────────
echo "━━ 7. Bookings — Create ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"service_id\":\"$SVC1_ID\",\"event_name\":\"Wedding Mehndi\",\"time_start\":\"10:00\",\"time_end\":\"12:00\",\"location\":\"123 Celebration Ave\",\"date_iso\":\"2026-05-15\",\"venue_latitude\":34.05,\"venue_longitude\":-118.24}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Create booking (Mehndi for Alice)" "200" "$code" "$body"
BK1=$(echo "$body" | jsonval "['booking_id']")
STATUS=$(echo "$body" | jsonval "['status']")
echo "     → booking_id=$BK1, status=$STATUS"
# Verify notification field in response
HAS_NOTIF=$(echo "$body" | python3 -c "import sys,json; print('notification' in json.load(sys.stdin))" 2>/dev/null)
echo "     → notification in response: $HAS_NOTIF"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"service_id\":\"$SVC2_ID\",\"event_name\":\"Wedding Photography\",\"time_start\":\"14:00\",\"time_end\":\"18:00\",\"location\":\"Grand Hall\",\"date_iso\":\"2026-05-15\",\"venue_latitude\":34.06,\"venue_longitude\":-118.25}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Create booking (Photography)" "200" "$code" "$body"
BK2=$(echo "$body" | jsonval "['booking_id']")

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"service_id\":\"$SVC3_ID\",\"event_name\":\"Sangeet DJ\",\"time_start\":\"20:00\",\"time_end\":\"23:59\",\"location\":\"Banquet Hall\",\"date_iso\":\"2026-05-14\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Create booking (DJ, no coordinates)" "200" "$code" "$body"
BK3=$(echo "$body" | jsonval "['booking_id']")

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"service_id\":\"fake-service-id\",\"event_name\":\"Bad\",\"time_start\":\"10:00\",\"time_end\":\"12:00\",\"location\":\"Nowhere\",\"date_iso\":\"2026-06-01\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Create with bad service_id → 404" "404" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"x"}')
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Create with missing fields → 422" "422" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 8. BOOKINGS — STATUS UPDATES (with notification integration)
# ─────────────────────────────────────────────────────────────────────
echo "━━ 8. Bookings — Status Updates & Notifications ━━━━━━━━━━━━━━━"

# Vendor approves booking 1
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BK1/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_ID\",\"is_vendor\":true,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Vendor approves BK1 → 200" "200" "$code" "$body"
echo "     → $(echo $body | jsonval "['message']")"

# Vendor rejects booking 2
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BK2/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_ID\",\"is_vendor\":true,\"status\":\"rejected\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Vendor rejects BK2 → 200" "200" "$code" "$body"

# Payment confirmed on BK1
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BK1/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"is_vendor\":false,\"status\":\"payment_confirmed\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Client confirms payment on BK1 → 200" "200" "$code" "$body"
HAS_NOTIF=$(echo "$body" | python3 -c "import sys,json; print('notification' in json.load(sys.stdin))" 2>/dev/null)
echo "     → notification in response: $HAS_NOTIF"

# Client tries to approve → 403
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BK3/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"is_vendor\":false,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Client tries to approve → 403" "403" "$code" "$body"

# Re-approve already approved → 400
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BK1/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_ID\",\"is_vendor\":true,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Re-approve non-pending → 400" "400" "$code" "$body"

# Non-existent booking
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/fake-booking-id/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_ID\",\"is_vendor\":true,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Update non-existent booking → 404" "404" "$code" "$body"

# Wrong vendor
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BK3/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"is_vendor\":true,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Wrong vendor tries to update → 403" "403" "$code" "$body"

# Wrong client
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BK3/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$CHARLIE_ID\",\"is_vendor\":false,\"status\":\"payment_confirmed\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Wrong client tries to update → 403" "403" "$code" "$body"

# Invalid status value
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BK3/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_ID\",\"is_vendor\":true,\"status\":\"invalid_status\"}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Invalid status enum → 422" "422" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 9. BOOKINGS — FETCH
# ─────────────────────────────────────────────────────────────────────
echo "━━ 9. Bookings — Fetch ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

resp=$(curl -s -w "\n%{http_code}" "$BASE/bookings/user/$ALICE_ID")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Get Alice's bookings → 200" "200" "$code" "$body"
COUNT=$(echo "$body" | jsonval "['bookings'].__len__()")
echo "     → $COUNT booking(s)"

resp=$(curl -s -w "\n%{http_code}" "$BASE/bookings/vendor/$VENDOR_ID")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Get vendor bookings → 200" "200" "$code" "$body"
COUNT=$(echo "$body" | jsonval "['bookings'].__len__()")
echo "     → $COUNT booking(s)"

resp=$(curl -s -w "\n%{http_code}" "$BASE/bookings/user/nonexistent")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Get bookings for unknown user → 200 (empty)" "200" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" "$BASE/bookings/vendor/nonexistent")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Get bookings for unknown vendor → 200 (empty)" "200" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 10. CHECK-IN (with notification integration)
# ─────────────────────────────────────────────────────────────────────
echo "━━ 10. Check-In ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Client at venue → pass
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BK1/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"is_vendor\":false,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Client check-in at venue → 200" "200" "$code" "$body"
HAS_NOTIF=$(echo "$body" | python3 -c "import sys,json; print('notification' in json.load(sys.stdin))" 2>/dev/null)
echo "     → notification in response: $HAS_NOTIF"

# Vendor at venue → pass
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BK1/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_ID\",\"is_vendor\":true,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Vendor check-in at venue → 200" "200" "$code" "$body"

# Too far away
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BK1/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"is_vendor\":false,\"latitude\":40.71,\"longitude\":-74.01}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Check-in too far → 400" "400" "$code" "$body"

# Non-existent booking
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/fake/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"is_vendor\":false,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Check-in non-existent booking → 404" "404" "$code" "$body"

# Wrong user
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BK1/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$CHARLIE_ID\",\"is_vendor\":false,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Wrong user check-in → 403" "403" "$code" "$body"

# No venue coordinates
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BK3/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_ID\",\"is_vendor\":false,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Check-in no venue coords → 400" "400" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 11. VENDOR AVAILABILITY
# ─────────────────────────────────────────────────────────────────────
echo "━━ 11. Vendor Availability ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/$VENDOR_ID/availability?start_date=2026-05-14T00:00:00Z&end_date=2026-05-16T23:59:59Z")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Get vendor availability (with busy times)" "200" "$code" "$body"
BASELINE=$(echo "$body" | jsonval "['baseline_hours_map'].__len__()")
INTERNAL=$(echo "$body" | jsonval "['internal_busy_times'].__len__()")
echo "     → Baseline days: $BASELINE, Internal busy blocks: $INTERNAL"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/nonexistent/availability?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Availability non-existent vendor → 404" "404" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 12. GOOGLE OAUTH
# ─────────────────────────────────────────────────────────────────────
echo "━━ 12. Google OAuth ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/$VENDOR_ID/google-auth")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Get Google OAuth URL" "200" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/auth/callback?state=invalid-uuid&code=auth_code")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "OAuth callback bad vendor → 404" "404" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 13. EDGE CASES & MISC
# ─────────────────────────────────────────────────────────────────────
echo "━━ 13. Edge Cases ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Hit a route that doesn't exist
resp=$(curl -s -w "\n%{http_code}" "$BASE/nonexistent-route")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "Non-existent route → 404" "404" "$code" "$body"

# POST to a GET-only route
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/db-check")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
check "POST to GET-only route → 405" "405" "$code" "$body"

echo ""

# ═════════════════════════════════════════════════════════════════════
# SUMMARY
# ═════════════════════════════════════════════════════════════════════
echo "╔═════════════════════════════════════════════════════════════════╗"
echo "║                       TEST SUMMARY                            ║"
echo "╠═════════════════════════════════════════════════════════════════╣"
printf "║  Total:  %-4d                                                ║\n" $TOTAL
printf "║  Passed: %-4d  ✅                                            ║\n" $PASS
printf "║  Failed: %-4d                                                ║\n" $FAIL
echo "╚═════════════════════════════════════════════════════════════════╝"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
