import { Star, MapPin, Award, TrendingUp, Plus, Check, Filter } from 'lucide-react';
import { useState } from 'react';
import type { Vendor } from '../App';

interface VendorBrowserProps {
  selectedVendors: Vendor[];
  onAddVendor: (vendor: Vendor) => void;
  onRemoveVendor: (vendorId: string) => void;
}

export function VendorBrowser({ selectedVendors, onAddVendor, onRemoveVendor }: VendorBrowserProps) {
  const [categoryFilter, setCategoryFilter] = useState<string>('all');
  const [locationFilter, setLocationFilter] = useState<string>('all');
  const [priceFilter, setPriceFilter] = useState<string>('all');

  // Mock vendor data
  const vendors: Vendor[] = [
    {
      id: '1',
      name: 'Beats & Bhangra DJ Services',
      category: 'DJ & Music',
      rating: 4.9,
      reviews: 156,
      location: 'San Francisco, CA',
      price: 1500,
      priceRange: '$$',
      image: 'dj',
      specialties: ['Weddings', 'Sangeet', 'Corporate Events'],
      yearsExperience: 8,
      eventsCompleted: 320,
      availability: ['2025-01-15', '2025-01-22', '2025-02-05'],
      description: 'Professional DJ with extensive Bollywood and Bhangra repertoire'
    },
    {
      id: '2',
      name: 'Spice & Soul Catering',
      category: 'Catering',
      rating: 5.0,
      reviews: 203,
      location: 'San Francisco, CA',
      price: 3200,
      priceRange: '$$$',
      image: 'catering',
      specialties: ['North Indian', 'South Indian', 'Fusion'],
      yearsExperience: 12,
      eventsCompleted: 580,
      availability: ['2025-01-15', '2025-01-22', '2025-02-05'],
      description: 'Award-winning caterer specializing in authentic South Asian cuisine'
    },
    {
      id: '3',
      name: 'Marigold Dreams Decor',
      category: 'Decoration',
      rating: 4.8,
      reviews: 128,
      location: 'San Francisco, CA',
      price: 2400,
      priceRange: '$$',
      image: 'decor',
      specialties: ['Floral', 'Mandap', 'Stage Setup'],
      yearsExperience: 6,
      eventsCompleted: 215,
      availability: ['2025-01-15', '2025-01-22'],
      description: 'Creating stunning traditional and modern décor arrangements'
    },
    {
      id: '4',
      name: 'Moments in Motion Photography',
      category: 'Photography',
      rating: 4.9,
      reviews: 187,
      location: 'San Francisco, CA',
      price: 2800,
      priceRange: '$$$',
      image: 'photography',
      specialties: ['Candid', 'Traditional', 'Cinematic'],
      yearsExperience: 10,
      eventsCompleted: 410,
      availability: ['2025-01-15', '2025-02-05'],
      description: 'Capturing the essence of your celebration through artistic storytelling'
    },
    {
      id: '5',
      name: 'Henna Heritage Artists',
      category: 'Mehndi',
      rating: 5.0,
      reviews: 241,
      location: 'Oakland, CA',
      price: 800,
      priceRange: '$',
      image: 'mehndi',
      specialties: ['Bridal Mehndi', 'Arabic', 'Traditional'],
      yearsExperience: 15,
      eventsCompleted: 890,
      availability: ['2025-01-15', '2025-01-22', '2025-02-05'],
      description: 'Master mehndi artists bringing intricate designs to life'
    },
    {
      id: '6',
      name: 'Royal Events Planning',
      category: 'Planning',
      rating: 4.9,
      reviews: 94,
      location: 'San Jose, CA',
      price: 1800,
      priceRange: '$$$',
      image: 'planning',
      specialties: ['Weddings', 'Multi-Day Events', 'Coordination'],
      yearsExperience: 9,
      eventsCompleted: 175,
      availability: ['2025-01-15', '2025-01-22'],
      description: 'Full-service planning and day-of coordination specialists'
    },
    {
      id: '7',
      name: 'Desi Beats Entertainment',
      category: 'DJ & Music',
      rating: 4.7,
      reviews: 112,
      location: 'Oakland, CA',
      price: 1200,
      priceRange: '$$',
      image: 'dj',
      specialties: ['Bollywood', 'Bhangra', 'EDM Fusion'],
      yearsExperience: 5,
      eventsCompleted: 180,
      availability: ['2025-01-22', '2025-02-05'],
      description: 'High-energy DJ sets that keep the dance floor packed'
    },
    {
      id: '8',
      name: 'Taj Mahal Catering Co.',
      category: 'Catering',
      rating: 4.8,
      reviews: 167,
      location: 'San Jose, CA',
      price: 2900,
      priceRange: '$$',
      image: 'catering',
      specialties: ['Pakistani', 'Indian', 'Bangladeshi'],
      yearsExperience: 14,
      eventsCompleted: 620,
      availability: ['2025-01-15', '2025-02-05'],
      description: 'Authentic regional cuisines with modern presentation'
    },
    {
      id: '9',
      name: 'Elegance in Frames',
      category: 'Photography',
      rating: 4.9,
      reviews: 145,
      location: 'Fremont, CA',
      price: 2400,
      priceRange: '$$',
      image: 'photography',
      specialties: ['Documentary', 'Portrait', 'Destination'],
      yearsExperience: 7,
      eventsCompleted: 290,
      availability: ['2025-01-22', '2025-02-05'],
      description: 'Documentary-style photography with artistic flair'
    },
    {
      id: '10',
      name: 'Lotus & Marigold Designs',
      category: 'Decoration',
      rating: 4.7,
      reviews: 98,
      location: 'Oakland, CA',
      price: 2100,
      priceRange: '$$',
      image: 'decor',
      specialties: ['Floral Arrangements', 'Lighting', 'Backdrops'],
      yearsExperience: 8,
      eventsCompleted: 245,
      availability: ['2025-01-15', '2025-01-22', '2025-02-05'],
      description: 'Transform your venue with breathtaking floral and lighting design'
    }
  ];

  const categories = ['all', 'DJ & Music', 'Catering', 'Decoration', 'Photography', 'Mehndi', 'Planning'];
  const locations = ['all', 'San Francisco, CA', 'Oakland, CA', 'San Jose, CA', 'Fremont, CA'];
  const priceRanges = ['all', '$', '$$', '$$$'];

  const filteredVendors = vendors.filter(vendor => {
    const matchesCategory = categoryFilter === 'all' || vendor.category === categoryFilter;
    const matchesLocation = locationFilter === 'all' || vendor.location === locationFilter;
    const matchesPrice = priceFilter === 'all' || vendor.priceRange === priceFilter;
    return matchesCategory && matchesLocation && matchesPrice;
  });

  const isVendorSelected = (vendorId: string) => {
    return selectedVendors.some(v => v.id === vendorId);
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-orange-50 to-white pb-20">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
        {/* Header */}
        <div className="text-center mb-12">
          <h1 className="mb-4 bg-gradient-to-r from-orange-600 to-pink-600 bg-clip-text text-transparent">
            Build Your Custom Bundle
          </h1>
          <p className="text-gray-600 max-w-2xl mx-auto mb-6">
            Browse and select vendors to create your perfect event bundle. Choose at least 2 vendors 
            (we recommend 3+) for a coordinated celebration.
          </p>
          
          {/* Bundle Status */}
          <div className="inline-flex items-center gap-3 px-6 py-3 bg-white rounded-xl border-2 border-orange-200 shadow-sm">
            <div className="flex items-center gap-2">
              <span className="text-gray-700">Vendors Selected:</span>
              <span className={`px-3 py-1 rounded-full text-sm ${
                selectedVendors.length >= 2 
                  ? 'bg-green-100 text-green-700' 
                  : 'bg-orange-100 text-orange-700'
              }`}>
                {selectedVendors.length}
              </span>
            </div>
            {selectedVendors.length < 2 && (
              <span className="text-sm text-gray-600">
                (Need {2 - selectedVendors.length} more to book)
              </span>
            )}
            {selectedVendors.length >= 2 && (
              <span className="text-sm text-green-600 flex items-center gap-1">
                <Check className="w-4 h-4" />
                Ready to book!
              </span>
            )}
          </div>
        </div>

        {/* Filters */}
        <div className="mb-8 bg-white p-6 rounded-2xl shadow-md border border-gray-200">
          <div className="flex items-center gap-2 mb-4">
            <Filter className="w-5 h-5 text-gray-600" />
            <h3 className="text-gray-900">Filter Vendors</h3>
          </div>
          
          <div className="grid md:grid-cols-3 gap-6">
            {/* Category Filter */}
            <div>
              <label className="block text-sm text-gray-600 mb-2">Category</label>
              <select
                value={categoryFilter}
                onChange={(e) => setCategoryFilter(e.target.value)}
                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
              >
                {categories.map(cat => (
                  <option key={cat} value={cat}>
                    {cat === 'all' ? 'All Categories' : cat}
                  </option>
                ))}
              </select>
            </div>

            {/* Location Filter */}
            <div>
              <label className="block text-sm text-gray-600 mb-2">Location</label>
              <select
                value={locationFilter}
                onChange={(e) => setLocationFilter(e.target.value)}
                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
              >
                {locations.map(loc => (
                  <option key={loc} value={loc}>
                    {loc === 'all' ? 'All Locations' : loc}
                  </option>
                ))}
              </select>
            </div>

            {/* Price Filter */}
            <div>
              <label className="block text-sm text-gray-600 mb-2">Price Range</label>
              <select
                value={priceFilter}
                onChange={(e) => setPriceFilter(e.target.value)}
                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500"
              >
                {priceRanges.map(price => (
                  <option key={price} value={price}>
                    {price === 'all' ? 'All Prices' : price}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {/* Results Count */}
        <div className="mb-6 text-gray-600">
          Showing {filteredVendors.length} vendor{filteredVendors.length !== 1 ? 's' : ''}
        </div>

        {/* Vendor Grid */}
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-8">
          {filteredVendors.map((vendor) => {
            const isSelected = isVendorSelected(vendor.id);
            
            return (
              <div
                key={vendor.id}
                className={`bg-white rounded-2xl shadow-lg hover:shadow-2xl transition-all overflow-hidden border-2 group ${
                  isSelected ? 'border-orange-500 ring-2 ring-orange-200' : 'border-gray-100'
                }`}
              >
                {/* Vendor Image */}
                <div className="relative h-48 bg-gradient-to-br from-orange-300 via-pink-300 to-purple-300 overflow-hidden">
                  <div className="absolute inset-0 bg-black/10"></div>
                  <div className="absolute inset-0 flex items-center justify-center text-white text-5xl">
                    {vendor.category === 'DJ & Music' ? '🎧' :
                     vendor.category === 'Catering' ? '🍛' :
                     vendor.category === 'Decoration' ? '🌸' :
                     vendor.category === 'Photography' ? '📸' :
                     vendor.category === 'Mehndi' ? '🌺' : '📋'}
                  </div>
                  {isSelected && (
                    <div className="absolute top-4 right-4 w-10 h-10 bg-gradient-to-r from-orange-500 to-pink-500 rounded-full flex items-center justify-center text-white shadow-lg">
                      <Check className="w-6 h-6" />
                    </div>
                  )}
                </div>

                <div className="p-6">
                  {/* Header */}
                  <div className="mb-4">
                    <h3 className="mb-2 text-gray-900">{vendor.name}</h3>
                    <div className="flex items-center gap-2 text-sm text-gray-600">
                      <span className="px-2 py-1 bg-orange-100 text-orange-700 rounded">
                        {vendor.category}
                      </span>
                      <span>{vendor.priceRange}</span>
                    </div>
                  </div>

                  {/* Rating & Location */}
                  <div className="flex items-center justify-between mb-4">
                    <div className="flex items-center gap-2">
                      <div className="flex items-center gap-1">
                        <Star className="w-4 h-4 fill-yellow-400 text-yellow-400" />
                        <span className="text-gray-900">{vendor.rating}</span>
                      </div>
                      <span className="text-sm text-gray-500">({vendor.reviews})</span>
                    </div>
                    <div className="flex items-center gap-1 text-sm text-gray-600">
                      <MapPin className="w-4 h-4" />
                      <span>{vendor.location.split(',')[0]}</span>
                    </div>
                  </div>

                  {/* Description */}
                  <p className="text-sm text-gray-600 mb-4">
                    {vendor.description}
                  </p>

                  {/* Specialties */}
                  <div className="mb-4">
                    <div className="flex flex-wrap gap-2">
                      {vendor.specialties.slice(0, 3).map((specialty, index) => (
                        <span
                          key={index}
                          className="px-2 py-1 bg-pink-50 text-pink-700 rounded text-xs"
                        >
                          {specialty}
                        </span>
                      ))}
                    </div>
                  </div>

                  {/* Stats */}
                  <div className="grid grid-cols-2 gap-4 mb-6 p-4 bg-gradient-to-br from-orange-50 to-pink-50 rounded-xl">
                    <div>
                      <div className="flex items-center gap-1 text-orange-600 mb-1">
                        <Award className="w-4 h-4" />
                        <span className="text-xs">Experience</span>
                      </div>
                      <div className="text-sm text-gray-900">{vendor.yearsExperience} years</div>
                    </div>
                    <div>
                      <div className="flex items-center gap-1 text-pink-600 mb-1">
                        <TrendingUp className="w-4 h-4" />
                        <span className="text-xs">Events</span>
                      </div>
                      <div className="text-sm text-gray-900">{vendor.eventsCompleted}+</div>
                    </div>
                  </div>

                  {/* Price & CTA */}
                  <div className="flex items-center justify-between pt-4 border-t border-gray-200">
                    <div>
                      <div className="text-sm text-gray-600">Starting at</div>
                      <div className="text-gray-900">${vendor.price.toLocaleString()}</div>
                    </div>
                    {isSelected ? (
                      <button
                        onClick={() => onRemoveVendor(vendor.id)}
                        className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 transition-all"
                      >
                        Remove
                      </button>
                    ) : (
                      <button
                        onClick={() => onAddVendor(vendor)}
                        className="px-4 py-2 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-lg hover:shadow-lg transition-all flex items-center gap-2"
                      >
                        <Plus className="w-4 h-4" />
                        Add to Bundle
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
