# 📦 PackageRoute — Smart Delivery Navigator

A Google Maps-style web app for finding the **shortest and all possible routes** for package delivery, powered by OpenRouteService Directions API.

---

## 🚀 Setup in 3 Steps

### 1. Get a Free API Key
- Go to [openrouteservice.org/dev/#/signup](https://openrouteservice.org/dev/#/signup)
- Sign up for free (2000 requests/day, no credit card)
- Copy your API key from the dashboard

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env and paste your API key
```

Or just open `app.py` and replace `YOUR_ORS_API_KEY_HERE` with your key.

### 3. Install & Run
```bash
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000** in your browser 🎉

---

## ✨ Features

| Feature | Details |
|---------|---------|
| 🗺️ Dark Map Interface | Google Maps-style with CartoDB dark tiles |
| 🔍 Geocoding | Type any city name — it auto-converts to coordinates |
| 🚗 Multiple Profiles | Car, Heavy Goods Vehicle, Bicycle, Walking |
| ↔️ Alternative Routes | Up to 3 alternatives per transport mode |
| ⚡ Fastest Route | Auto-highlighted in green, shown at the top |
| 📋 All Routes Listed | Sorted by travel time with full metrics |
| 🗺️ Turn-by-Turn | Step-by-step directions for selected route |
| 📍 Click to Select | Click any route card or polyline to highlight it |

---

## 📁 Project Structure

```
package-route/
├── app.py              ← Flask backend (API routes + geocoding)
├── requirements.txt    ← Python dependencies
├── .env.example        ← Environment template
├── .env                ← Your API key (create this)
└── static/
    └── index.html      ← Full frontend (Leaflet map + UI)
```

---

## 🔌 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /` | GET | Serve the frontend |
| `GET /api/geocode?place=Mumbai` | GET | Convert place name to coordinates |
| `POST /api/routes` | POST | Get all routes between two points |
| `GET /api/profiles` | GET | List available transport profiles |

### Example POST /api/routes
```json
{
  "start": "Mumbai, India",
  "end": "Pune, India"
}
```

---

## 🛠️ Extending the Project

- **Add Google Maps API**: Replace ORS with Google Directions API for more route options
- **Database**: Store route history with SQLite/PostgreSQL
- **Real-time tracking**: Add WebSockets for live package location
- **Cost estimation**: Add fuel/toll cost calculation per route
- **Export**: Add PDF/CSV export of route details

---

## 📝 Notes
- OpenRouteService covers most of the world's road network
- For India, coverage is excellent in major cities
- Alternative routes availability depends on the road network density
