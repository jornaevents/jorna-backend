#!/bin/bash
# Comprehensive Live API Test Script for Desiconnect
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
    echo "✅ PASS [$actual_code] $desc"
  else
    FAIL=$((FAIL + 1))
    echo "❌ FAIL [$actual_code expected $expected_code] $desc"
    echo "   Response: $body"
  fi
}

echo "================================================================="
echo "  DESICONNECT API — COMPREHENSIVE LIVE TEST"
echo "================================================================="
echo ""

# ─────────────────────────────────────────────────────────────────────
# 1. ROOT & HEALTH
# ─────────────────────────────────────────────────────────────────────
echo "── 1. Root & Health ──"

resp=$(curl -s -w "\n%{http_code}" "$BASE/")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "GET / returns 200" "200" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" "$BASE/db-check")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "GET /db-check returns 200" "200" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 2. AUTH — REGISTER
# ─────────────────────────────────────────────────────────────────────
echo "── 2. Auth — Register ──"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@test.com","password":"Pass1234","username":"alice","phone":"5550001","f_name":"Alice","l_name":"Smith","age":25,"location":"10001","gender":"Female","language":"English"}')
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Register Alice" "200" "$code" "$body"
ALICE_USER_ID=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['user_id'])" 2>/dev/null || echo "UNKNOWN")
echo "   → alice user_id=$ALICE_USER_ID"

# Register vendor user
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"bob_vendor@test.com","password":"VendorPass!","username":"bob_vendor","phone":"5550002","f_name":"Bob","l_name":"Kumar","age":35,"location":"90001","gender":"Male","language":"Hindi"}')
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Register Bob (future vendor)" "200" "$code" "$body"
BOB_USER_ID=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['user_id'])" 2>/dev/null || echo "UNKNOWN")
echo "   → bob user_id=$BOB_USER_ID"

# Duplicate registration
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@test.com","password":"Pass1234","username":"alice","phone":"5550001","f_name":"Alice","l_name":"Smith","age":25,"location":"10001","gender":"Female","language":"English"}')
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Duplicate email/username → 400" "400" "$code" "$body"

# Missing required fields
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"bad@test.com"}')
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Missing fields → 422" "422" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 3. AUTH — LOGIN
# ─────────────────────────────────────────────────────────────────────
echo "── 3. Auth — Login ──"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@test.com","password":"Pass1234"}')
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Login Alice (valid)" "200" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@test.com","password":"WRONG"}')
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Login wrong password → 401" "401" "$code" "$body"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"ghost@test.com","password":"nope"}')
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Login non-existent email → 401" "401" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 4. SEED VENDOR + SERVICE directly via DB (using Python helper)
# ─────────────────────────────────────────────────────────────────────
echo "── 4. Seed Vendor/Service Data ──"

SEED_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '.')
from app.db.database import SessionLocal
from app.db.models import Vendor, Service, User, VendorAvailability
import json

db = SessionLocal()

# Update Bob with lat/lon
bob = db.query(User).filter(User.email == 'bob_vendor@test.com').first()
bob.latitude = 34.05
bob.longitude = -118.24
bob.city = 'Los Angeles'
bob.state = 'CA'
db.commit()

vendor = Vendor(
    user_id=bob.user_id, bio='Expert Mehndi artist & photographer', 
    rating=4.8, num_events=50, travel_radius_miles=30
)
db.add(vendor)
db.commit()
db.refresh(vendor)

svc1 = Service(name='Mehndi', price=300.0, duration_minutes=90, vendor_id=vendor.vendor_id, experience='10 years')
svc2 = Service(name='Photography', price=1200.0, duration_minutes=180, vendor_id=vendor.vendor_id, experience='8 years')
db.add(svc1)
db.add(svc2)
db.commit()
db.refresh(svc1)
db.refresh(svc2)

# Add baseline availability (Mon-Fri 9-5)
for day in range(5):
    avail = VendorAvailability(vendor_id=vendor.vendor_id, day_of_week=day, start_time='09:00', end_time='17:00')
    db.add(avail)
db.commit()

print(json.dumps({
    'vendor_id': vendor.vendor_id,
    'service1_id': svc1.service_id,
    'service2_id': svc2.service_id,
    'bob_user_id': bob.user_id
}))
db.close()
" 2>&1)

echo "   Seed result: $SEED_RESULT"
VENDOR_ID=$(echo "$SEED_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['vendor_id'])" 2>/dev/null || echo "UNKNOWN")
SERVICE1_ID=$(echo "$SEED_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['service1_id'])" 2>/dev/null || echo "UNKNOWN")
SERVICE2_ID=$(echo "$SEED_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['service2_id'])" 2>/dev/null || echo "UNKNOWN")
echo "   → vendor_id=$VENDOR_ID"
echo "   → service1_id=$SERVICE1_ID  (Mehndi)"
echo "   → service2_id=$SERVICE2_ID  (Photography)"
echo ""

# ─────────────────────────────────────────────────────────────────────
# 5. VENDOR SEARCH
# ─────────────────────────────────────────────────────────────────────
echo "── 5. Vendor Search ──"

# Nearby search (within 30 mile radius)
resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=Mehndi&latitude=34.06&longitude=-118.25")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Search Mehndi near LA → found" "200" "$code" "$body"
echo "   → $(echo $body | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"Found {len(d[\"vendors\"])} vendor(s)")' 2>/dev/null)"

# Far away search (should be empty)
resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=Mehndi&latitude=40.71&longitude=-74.01")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Search Mehndi in NYC → empty (too far)" "200" "$code" "$body"
echo "   → $(echo $body | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"Found {len(d[\"vendors\"])} vendor(s)")' 2>/dev/null)"

# Partial name search
resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=Photo&latitude=34.06&longitude=-118.25")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Search 'Photo' partial match" "200" "$code" "$body"
echo "   → $(echo $body | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"Found {len(d[\"vendors\"])} vendor(s)")' 2>/dev/null)"

# Non-existent service
resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/search?service_name=Xyz123&latitude=34.06&longitude=-118.25")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Search non-existent service → empty" "200" "$code" "$body"
echo "   → $(echo $body | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"Found {len(d[\"vendors\"])} vendor(s)")' 2>/dev/null)"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 6. BOOKINGS — CREATE
# ─────────────────────────────────────────────────────────────────────
echo "── 6. Bookings — Create ──"

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"service_id\":\"$SERVICE1_ID\",\"event_name\":\"Wedding Mehndi\",\"time_start\":\"10:00\",\"time_end\":\"12:00\",\"location\":\"123 Celebration Ave\",\"date_iso\":\"2026-05-15\",\"venue_latitude\":34.05,\"venue_longitude\":-118.24}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Create booking (Mehndi for Alice)" "200" "$code" "$body"
BOOKING1_ID=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['booking_id'])" 2>/dev/null || echo "UNKNOWN")
echo "   → booking_id=$BOOKING1_ID, status=$(echo $body | python3 -c 'import sys,json; print(json.load(sys.stdin).get(\"status\",\"?\"))' 2>/dev/null)"

# Second booking
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"service_id\":\"$SERVICE2_ID\",\"event_name\":\"Wedding Photography\",\"time_start\":\"14:00\",\"time_end\":\"18:00\",\"location\":\"456 Grand Hall\",\"date_iso\":\"2026-05-15\",\"venue_latitude\":34.06,\"venue_longitude\":-118.25}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Create booking (Photography for Alice)" "200" "$code" "$body"
BOOKING2_ID=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['booking_id'])" 2>/dev/null || echo "UNKNOWN")
echo "   → booking_id=$BOOKING2_ID"

# Non-existent service
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"service_id\":\"fake-service-id\",\"event_name\":\"Bad Event\",\"time_start\":\"10:00\",\"time_end\":\"12:00\",\"location\":\"Nowhere\",\"date_iso\":\"2026-06-01\"}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Create booking w/ bad service_id → 404" "404" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 7. BOOKINGS — STATUS UPDATE
# ─────────────────────────────────────────────────────────────────────
echo "── 7. Bookings — Status Update ──"

# Vendor approves booking 1
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BOOKING1_ID/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_USER_ID\",\"is_vendor\":true,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Vendor approves booking 1" "200" "$code" "$body"

# Vendor rejects booking 2
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BOOKING2_ID/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_USER_ID\",\"is_vendor\":true,\"status\":\"rejected\"}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Vendor rejects booking 2" "200" "$code" "$body"

# Client tries to approve → 403
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BOOKING1_ID/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"is_vendor\":false,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Client tries to approve → 403" "403" "$code" "$body"

# Try to approve already-approved booking → 400
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BOOKING1_ID/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_USER_ID\",\"is_vendor\":true,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Re-approve already approved → 400" "400" "$code" "$body"

# Non-existent booking
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/fake-booking-id/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_USER_ID\",\"is_vendor\":true,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Update non-existent booking → 404" "404" "$code" "$body"

# Wrong vendor tries to update
resp=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/bookings/$BOOKING1_ID/status" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"is_vendor\":true,\"status\":\"approved\"}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Wrong vendor tries to update → 403" "403" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 8. BOOKINGS — FETCH BY USER & VENDOR
# ─────────────────────────────────────────────────────────────────────
echo "── 8. Bookings — Fetch ──"

resp=$(curl -s -w "\n%{http_code}" "$BASE/bookings/user/$ALICE_USER_ID")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Get Alice's bookings" "200" "$code" "$body"
echo "   → $(echo $body | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"Found {len(d[\"bookings\"])} booking(s)")' 2>/dev/null)"

resp=$(curl -s -w "\n%{http_code}" "$BASE/bookings/vendor/$VENDOR_ID")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Get Bob's vendor bookings" "200" "$code" "$body"
echo "   → $(echo $body | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"Found {len(d[\"bookings\"])} booking(s)")' 2>/dev/null)"

# Empty user
resp=$(curl -s -w "\n%{http_code}" "$BASE/bookings/user/nonexistent-user-id")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Get bookings for unknown user → 200 empty" "200" "$code" "$body"
echo "   → $(echo $body | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"Found {len(d[\"bookings\"])} booking(s)")' 2>/dev/null)"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 9. CHECK-IN
# ─────────────────────────────────────────────────────────────────────
echo "── 9. Check-In ──"

# Client at the venue → success
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BOOKING1_ID/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"is_vendor\":false,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Client check-in at venue → 200" "200" "$code" "$body"

# Vendor at the venue → success
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BOOKING1_ID/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$BOB_USER_ID\",\"is_vendor\":true,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Vendor check-in at venue → 200" "200" "$code" "$body"

# Too far away → error
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BOOKING1_ID/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"is_vendor\":false,\"latitude\":40.71,\"longitude\":-74.01}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Check-in too far away → 400" "400" "$code" "$body"

# Non-existent booking
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/fake-id/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"is_vendor\":false,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Check-in non-existent booking → 404" "404" "$code" "$body"

# Wrong user check in → 403
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BOOKING1_ID/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"some-random-user\",\"is_vendor\":false,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Wrong user check-in → 403" "403" "$code" "$body"

# Booking without coordinates
# Create a booking without venue coordinates first
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"service_id\":\"$SERVICE1_ID\",\"event_name\":\"No Coord Event\",\"time_start\":\"10:00\",\"time_end\":\"12:00\",\"location\":\"Somewhere\",\"date_iso\":\"2026-06-01\"}")
code_create=$(echo "$resp" | tail -1)
body_create=$(echo "$resp" | sed '$d')
BOOKING_NOCOORD_ID=$(echo "$body_create" | python3 -c "import sys,json; print(json.load(sys.stdin)['booking_id'])" 2>/dev/null || echo "UNKNOWN")

resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE/bookings/$BOOKING_NOCOORD_ID/check-in" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$ALICE_USER_ID\",\"is_vendor\":false,\"latitude\":34.05,\"longitude\":-118.24}")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Check-in w/o venue coordinates → 400" "400" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 10. VENDOR AVAILABILITY
# ─────────────────────────────────────────────────────────────────────
echo "── 10. Vendor Availability ──"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/$VENDOR_ID/availability?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Get vendor availability" "200" "$code" "$body"
echo "   → $(echo $body | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"Baseline days: {len(d[\"baseline_hours_map\"])}, Internal busy: {len(d[\"internal_busy_times\"])}, Google busy: {len(d[\"google_busy_times\"])}")' 2>/dev/null)"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/nonexistent-vendor/availability?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Availability for non-existent vendor → 404" "404" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# 11. GOOGLE AUTH REDIRECT
# ─────────────────────────────────────────────────────────────────────
echo "── 11. Google Auth ──"

resp=$(curl -s -w "\n%{http_code}" "$BASE/vendors/$VENDOR_ID/google-auth")
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')
check "Get Google OAuth URL" "200" "$code" "$body"

echo ""

# ─────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────
echo "================================================================="
echo "  TEST SUMMARY"
echo "================================================================="
echo "  Total:  $TOTAL"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo "================================================================="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
