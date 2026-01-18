import { Menu, X, Sparkles, ShoppingCart } from 'lucide-react';
import { useState } from 'react';

interface NavigationProps {
  activeView: 'home' | 'browse' | 'planner';
  setActiveView: (view: 'home' | 'browse' | 'planner') => void;
  bundleCount: number;
  onCartClick: () => void;
}

export function Navigation({ activeView, setActiveView, bundleCount, onCartClick }: NavigationProps) {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const navItems = [
    { id: 'home' as const, label: 'Home' },
    { id: 'browse' as const, label: 'Browse Vendors' },
    { id: 'planner' as const, label: 'AI Planner', icon: Sparkles },
  ];

  return (
    <nav className="fixed top-0 left-0 right-0 z-50 bg-white/95 backdrop-blur-sm border-b border-orange-100 shadow-sm">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex justify-between items-center h-16">
          {/* Logo */}
          <button
            onClick={() => setActiveView('home')}
            className="flex items-center space-x-2 group"
          >
            <div className="w-10 h-10 bg-gradient-to-br from-orange-500 to-pink-500 rounded-lg flex items-center justify-center transform group-hover:scale-105 transition-transform">
              <span className="text-white">DC</span>
            </div>
            <span className="bg-gradient-to-r from-orange-600 to-pink-600 bg-clip-text text-transparent">
              DesiConnect
            </span>
          </button>

          {/* Desktop Navigation */}
          <div className="hidden md:flex items-center space-x-1">
            {navItems.map((item) => (
              <button
                key={item.id}
                onClick={() => setActiveView(item.id)}
                className={`px-4 py-2 rounded-lg transition-all flex items-center gap-2 ${
                  activeView === item.id
                    ? 'bg-gradient-to-r from-orange-500 to-pink-500 text-white'
                    : 'text-gray-700 hover:bg-orange-50'
                }`}
              >
                {item.icon && <item.icon className="w-4 h-4" />}
                {item.label}
              </button>
            ))}
          </div>

          {/* CTA Buttons */}
          <div className="hidden md:flex items-center space-x-3">
            <button 
              onClick={onCartClick}
              className="relative px-4 py-2 text-gray-700 hover:text-orange-600 transition-colors flex items-center gap-2"
            >
              <ShoppingCart className="w-5 h-5" />
              <span>My Bundle</span>
              {bundleCount > 0 && (
                <span className="absolute -top-1 -right-1 w-5 h-5 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-full text-xs flex items-center justify-center">
                  {bundleCount}
                </span>
              )}
            </button>
            <button className="px-4 py-2 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-lg hover:shadow-lg transition-all">
              Get Started
            </button>
          </div>

          {/* Mobile Menu Button */}
          <button
            onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
            className="md:hidden p-2 rounded-lg hover:bg-orange-50 relative"
          >
            {bundleCount > 0 && (
              <span className="absolute top-0 right-0 w-4 h-4 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-full text-xs flex items-center justify-center">
                {bundleCount}
              </span>
            )}
            {mobileMenuOpen ? <X className="w-6 h-6" /> : <Menu className="w-6 h-6" />}
          </button>
        </div>

        {/* Mobile Menu */}
        {mobileMenuOpen && (
          <div className="md:hidden py-4 border-t border-orange-100">
            <div className="flex flex-col space-y-2">
              {navItems.map((item) => (
                <button
                  key={item.id}
                  onClick={() => {
                    setActiveView(item.id);
                    setMobileMenuOpen(false);
                  }}
                  className={`px-4 py-3 rounded-lg transition-all text-left flex items-center gap-2 ${
                    activeView === item.id
                      ? 'bg-gradient-to-r from-orange-500 to-pink-500 text-white'
                      : 'text-gray-700 hover:bg-orange-50'
                  }`}
                >
                  {item.icon && <item.icon className="w-4 h-4" />}
                  {item.label}
                </button>
              ))}
              <div className="pt-4 space-y-2">
                <button 
                  onClick={() => {
                    onCartClick();
                    setMobileMenuOpen(false);
                  }}
                  className="w-full px-4 py-3 text-gray-700 hover:bg-orange-50 rounded-lg flex items-center justify-between"
                >
                  <span className="flex items-center gap-2">
                    <ShoppingCart className="w-5 h-5" />
                    My Bundle
                  </span>
                  {bundleCount > 0 && (
                    <span className="px-2 py-1 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-full text-xs">
                      {bundleCount}
                    </span>
                  )}
                </button>
                <button className="w-full px-4 py-3 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-lg">
                  Get Started
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </nav>
  );
}