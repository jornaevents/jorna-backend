import { useState } from 'react';
import { Hero } from './components/Hero';
import { VendorBrowser } from './components/VendorBrowser';
import { AIEventPlanner } from './components/AIEventPlanner';
import { BundleCart } from './components/BundleCart';
import { HowItWorks } from './components/HowItWorks';
import { Navigation } from './components/Navigation';

export interface Vendor {
  id: string;
  name: string;
  category: string;
  rating: number;
  reviews: number;
  location: string;
  price: number;
  priceRange: string;
  image: string;
  specialties: string[];
  yearsExperience: number;
  eventsCompleted: number;
  availability: string[];
  description: string;
}

export default function App() {
  const [activeView, setActiveView] = useState<'home' | 'browse' | 'planner'>('home');
  const [selectedVendors, setSelectedVendors] = useState<Vendor[]>([]);
  const [showCart, setShowCart] = useState(false);

  const addVendorToBundle = (vendor: Vendor) => {
    if (!selectedVendors.find(v => v.id === vendor.id)) {
      setSelectedVendors([...selectedVendors, vendor]);
      setShowCart(true);
    }
  };

  const removeVendorFromBundle = (vendorId: string) => {
    setSelectedVendors(selectedVendors.filter(v => v.id !== vendorId));
  };

  const clearBundle = () => {
    setSelectedVendors([]);
  };

  const setAIGeneratedBundle = (vendors: Vendor[]) => {
    setSelectedVendors(vendors);
    setShowCart(true);
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-orange-50 via-white to-pink-50">
      <Navigation 
        activeView={activeView} 
        setActiveView={setActiveView}
        bundleCount={selectedVendors.length}
        onCartClick={() => setShowCart(true)}
      />
      
      {activeView === 'home' && (
        <>
          <Hero setActiveView={setActiveView} />
          <HowItWorks />
        </>
      )}
      
      {activeView === 'browse' && (
        <div className="pt-20">
          <VendorBrowser 
            selectedVendors={selectedVendors}
            onAddVendor={addVendorToBundle}
            onRemoveVendor={removeVendorFromBundle}
          />
        </div>
      )}
      
      {activeView === 'planner' && (
        <div className="pt-20">
          <AIEventPlanner onGenerateBundle={setAIGeneratedBundle} />
        </div>
      )}

      {/* Bundle Cart Sidebar */}
      <BundleCart
        isOpen={showCart}
        onClose={() => setShowCart(false)}
        vendors={selectedVendors}
        onRemoveVendor={removeVendorFromBundle}
        onClearBundle={clearBundle}
      />
      
      <footer className="bg-gradient-to-r from-orange-600 to-pink-600 text-white py-12 mt-20">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid grid-cols-1 md:grid-cols-4 gap-8">
            <div>
              <h3 className="mb-4">DesiConnect</h3>
              <p className="text-orange-100 text-sm">
                Modernizing South Asian event planning through dynamic vendor coordination.
              </p>
            </div>
            <div>
              <h4 className="mb-4">For Clients</h4>
              <ul className="space-y-2 text-sm text-orange-100">
                <li>Browse Vendors</li>
                <li>AI Event Planner</li>
                <li>Build Your Bundle</li>
                <li>How It Works</li>
              </ul>
            </div>
            <div>
              <h4 className="mb-4">For Vendors</h4>
              <ul className="space-y-2 text-sm text-orange-100">
                <li>Become a Partner</li>
                <li>Vendor Dashboard</li>
                <li>Pricing</li>
                <li>Resources</li>
              </ul>
            </div>
            <div>
              <h4 className="mb-4">Support</h4>
              <ul className="space-y-2 text-sm text-orange-100">
                <li>Contact Us</li>
                <li>FAQs</li>
                <li>Terms of Service</li>
                <li>Privacy Policy</li>
              </ul>
            </div>
          </div>
          <div className="border-t border-orange-400 mt-8 pt-8 text-center text-sm text-orange-100">
            © 2025 DesiConnect. Bringing communities together through celebration.
          </div>
        </div>
      </footer>
    </div>
  );
}
