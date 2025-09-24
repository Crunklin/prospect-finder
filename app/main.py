import os
import secrets
import json
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta

from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import httpx
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI(title="Fleet Prospect Finder", version="1.0.0")

# Setup authentication
security = HTTPBasic()

def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    """Simple authentication check"""
    correct_username = os.getenv("APP_USERNAME", "admin")
    correct_password = os.getenv("APP_PASSWORD", "changeme123")
    
    is_correct_username = secrets.compare_digest(credentials.username, correct_username)
    is_correct_password = secrets.compare_digest(credentials.password, correct_password)
    
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:8003",
        "https://crunklin.github.io",
        "https://Crunklin.github.io"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models
class SearchRequest(BaseModel):
    location: str
    categories: List[str]
    radius: int = 5000
    exclude_service_area_only: bool = True

class BusinessResult(BaseModel):
    name: str
    address: str
    city: str
    state: str
    zip_code: str
    phone: Optional[str] = None
    website: Optional[str] = None
    rating: Optional[float] = None
    total_ratings: Optional[int] = None
    category: str
    distance: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

class SearchResponse(BaseModel):
    results: List[BusinessResult]
    total_count: int
    search_info: Dict[str, Any]

# Cache for search results (simple in-memory cache)
search_cache = {}
CACHE_DURATION = timedelta(minutes=20)

# Google Places API configuration
PLACES_API_KEY = os.getenv("PLACES_API_KEY")
if not PLACES_API_KEY:
    print("WARNING: PLACES_API_KEY not found in environment variables")

def load_category_packs():
    """Load category packs from JSON file"""
    try:
        data_path = Path("data/categories.json")
        if not data_path.exists():
            # Fallback category data if file doesn't exist
            return {
                "Automotive & Fleet Core": [
                    "car_dealer", "car_rental", "car_repair", "gas_station"
                ],
                "Home / Field Services": [
                    "electrician", "plumber", "roofing_contractor", "painter"
                ],
                "Logistics / Mobility": [
                    "moving_company", "storage", "taxi_service", "logistics"
                ],
                "Industrial / Construction Ops": [
                    "general_contractor", "electrician", "plumber", "hardware_store"
                ],
                "Recreation": [
                    "amusement_park", "zoo", "aquarium", "tourist_attraction"
                ]
            }
        
        with open(data_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading categories: {e}")
        # Return fallback data
        return {
            "Automotive & Fleet Core": [
                "car_dealer", "car_rental", "car_repair", "gas_station"
            ],
            "Home / Field Services": [
                "electrician", "plumber", "roofing_contractor", "painter"
            ]
        }

# Load categories
CATEGORY_PACKS = load_category_packs()

async def geocode_location(location: str) -> Dict[str, float]:
    """Convert location string to lat/lng coordinates"""
    if not PLACES_API_KEY:
        raise HTTPException(status_code=500, detail="Google Places API key not configured")
    
    cache_key = f"geocode_{location.lower()}"
    if cache_key in search_cache:
        cached_result, timestamp = search_cache[cache_key]
        if datetime.now() - timestamp < CACHE_DURATION:
            return cached_result
    
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": location,
        "key": PLACES_API_KEY
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        data = response.json()
    
    if data["status"] != "OK" or not data["results"]:
        raise HTTPException(status_code=400, detail=f"Could not geocode location: {location}")
    
    result = data["results"][0]
    coords = {
        "lat": result["geometry"]["location"]["lat"],
        "lng": result["geometry"]["location"]["lng"]
    }
    
    # Cache the result
    search_cache[cache_key] = (coords, datetime.now())
    return coords

async def search_places_nearby(lat: float, lng: float, place_types: List[str], radius: int) -> List[Dict]:
    """Search for places using Google Places API"""
    if not PLACES_API_KEY:
        raise HTTPException(status_code=500, detail="Google Places API key not configured")
    
    all_results = []
    
    for place_type in place_types:
        cache_key = f"search_{lat}_{lng}_{place_type}_{radius}"
        if cache_key in search_cache:
            cached_result, timestamp = search_cache[cache_key]
            if datetime.now() - timestamp < CACHE_DURATION:
                all_results.extend(cached_result)
                continue
        
        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{lat},{lng}",
            "radius": radius,
            "type": place_type,
            "key": PLACES_API_KEY
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            data = response.json()
        
        if data["status"] == "OK":
            # Cache the results
            search_cache[cache_key] = (data["results"], datetime.now())
            all_results.extend(data["results"])
    
    return all_results

def calculate_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance between two points in miles"""
    from math import radians, cos, sin, asin, sqrt
    
    # Convert to radians
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    
    # Haversine formula
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlng/2)**2
    c = 2 * asin(sqrt(a))
    r = 3959  # Radius of earth in miles
    return c * r

# Routes

@app.get("/health")
async def health_check():
    """Health check endpoint (unprotected)"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/")
async def serve_app(user: str = Depends(get_current_user)):
    """Serve the main application (protected)"""
    return FileResponse('app/web/index.html')

@app.get("/categories")
async def get_categories(user: str = Depends(get_current_user)):
    """Get available business categories (protected)"""
    return CATEGORY_PACKS

@app.post("/search/places", response_model=SearchResponse)
async def search_places(request: SearchRequest, user: str = Depends(get_current_user)) -> SearchResponse:
    """Search for places based on location and categories (protected)"""
    try:
        # Geocode the location
        coords = await geocode_location(request.location)
        
        # Get all place types for selected categories
        place_types = []
        for category_pack in request.categories:
            if category_pack in CATEGORY_PACKS:
                place_types.extend(CATEGORY_PACKS[category_pack])
        
        if not place_types:
            return SearchResponse(
                results=[],
                total_count=0,
                search_info={
                    "location": request.location,
                    "coordinates": coords,
                    "message": "No valid categories selected"
                }
            )
        
        # Search for places
        places = await search_places_nearby(
            coords["lat"], 
            coords["lng"], 
            place_types, 
            request.radius
        )
        
        # Process results
        results = []
        seen_places = set()  # To avoid duplicates
        
        for place in places:
            place_id = place.get("place_id")
            if place_id in seen_places:
                continue
            seen_places.add(place_id)
            
            # Filter out service area only businesses if requested
            if request.exclude_service_area_only:
                if not place.get("geometry", {}).get("location"):
                    continue
            
            # Extract address components
            address_components = place.get("vicinity", "")
            formatted_address = place.get("formatted_address", address_components)
            
            # Parse address (basic parsing)
            address_parts = formatted_address.split(",") if formatted_address else [""]
            address = address_parts[0].strip() if address_parts else ""
            city = address_parts[1].strip() if len(address_parts) > 1 else ""
            state_zip = address_parts[2].strip() if len(address_parts) > 2 else ""
            
            # Extract state and zip (basic regex could be added here)
            state = state_zip.split()[0] if state_zip else ""
            zip_code = ""
            if state_zip:
                zip_parts = state_zip.split()
                if len(zip_parts) > 1:
                    zip_code = zip_parts[-1]
            
            # Calculate distance
            place_location = place.get("geometry", {}).get("location", {})
            distance = None
            if place_location:
                distance = calculate_distance(
                    coords["lat"], coords["lng"],
                    place_location["lat"], place_location["lng"]
                )
            
            # Determine category
            place_types_list = place.get("types", [])
            category = "Unknown"
            for pack_name, pack_types in CATEGORY_PACKS.items():
                if any(pt in place_types_list for pt in pack_types):
                    category = pack_name
                    break
            
            result = BusinessResult(
                name=place.get("name", "Unknown"),
                address=address,
                city=city,
                state=state,
                zip_code=zip_code,
                phone=None,  # Would need Places Details API for phone
                website=None,  # Would need Places Details API for website
                rating=place.get("rating"),
                total_ratings=place.get("user_ratings_total"),
                category=category,
                distance=round(distance, 2) if distance else None,
                lat=place_location.get("lat") if place_location else None,
                lng=place_location.get("lng") if place_location else None
            )
            results.append(result)
        
        # Sort by distance
        results.sort(key=lambda x: x.distance or float('inf'))
        
        return SearchResponse(
            results=results,
            total_count=len(results),
            search_info={
                "location": request.location,
                "coordinates": coords,
                "radius_miles": request.radius * 0.000621371,  # Convert meters to miles
                "categories_searched": request.categories,
                "place_types": place_types
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

# Mount static files (for CSS, JS, etc.)
if Path("app/web").exists():
    app.mount("/static", StaticFiles(directory="app/web"), name="static")

# Serve individual static files
@app.get("/{file_path:path}")
async def serve_static_files(file_path: str, user: str = Depends(get_current_user)):
    """Serve static files (protected)"""
    static_file_path = Path(f"app/web/{file_path}")
    if static_file_path.exists() and static_file_path.is_file():
        return FileResponse(static_file_path)
    raise HTTPException(status_code=404, detail="File not found")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)