import math

# How far a venue may sit from the event's location and still be offered.
# A venue is not a vendor who travels to you — it *is* where the event happens —
# so the vendor's own travel_radius_miles says nothing about it. This is the
# client's willingness to drive, which is a property of the event, not the
# supplier. Lives here so search and the bundle builder can't drift apart.
VENUE_MAX_DISTANCE_MILES = 50


def calculate_distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two geographic coordinates
    using the Haversine formula, and return the distance in miles.
    """
    # Convert decimal degrees to radians
    lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])

    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    # Radius of earth in miles is approximately 3958.8 miles
    radius_miles = 3958.8
    # Calculate and return distance
    return c * radius_miles
