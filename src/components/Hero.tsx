import { Calendar, Users, Sparkles, ArrowRight } from 'lucide-react';

interface HeroProps {
  setActiveView: (view: 'home' | 'browse' | 'planner') => void;
}

export function Hero({ setActiveView }: HeroProps) {
  return (
    <div className="relative overflow-hidden pt-16">
      {/* Background Pattern */}
      <div className="absolute inset-0 bg-gradient-to-br from-orange-100 via-pink-50 to-purple-100 opacity-50">
        <div className="absolute inset-0" style={{
          backgroundImage: `radial-gradient(circle at 20px 20px, rgba(251, 146, 60, 0.1) 1px, transparent 1px)`,
          backgroundSize: '40px 40px'
        }}></div>
      </div>

      <div className="relative max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 lg:py-28">
        <div className="grid lg:grid-cols-2 gap-12 items-center">
          {/* Left Column - Content */}
          <div className="space-y-8">
            <div className="inline-flex items-center gap-2 px-4 py-2 bg-white/80 backdrop-blur-sm rounded-full border border-orange-200">
              <Sparkles className="w-4 h-4 text-orange-500" />
              <span className="text-sm text-gray-700">Dynamic Bundle Creation</span>
            </div>

            <h1 className="bg-gradient-to-r from-orange-600 via-pink-600 to-purple-600 bg-clip-text text-transparent">
              Build Your Perfect Event Bundle—Your Way
            </h1>

            <p className="text-gray-600 text-lg">
              Coordinate multiple verified South Asian vendors in one place. Build custom bundles manually 
              or let AI match you with the perfect combination based on your event details, budget, and preferences.
            </p>

            <div className="flex flex-col sm:flex-row gap-4">
              <button
                onClick={() => setActiveView('planner')}
                className="px-8 py-4 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-xl hover:shadow-xl transition-all flex items-center justify-center gap-2 group"
              >
                <Sparkles className="w-5 h-5" />
                Start with AI Planner
                <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
              </button>
              <button
                onClick={() => setActiveView('browse')}
                className="px-8 py-4 bg-white border-2 border-orange-300 text-gray-700 rounded-xl hover:bg-orange-50 transition-all"
              >
                Browse & Build Manually
              </button>
            </div>

            {/* Stats */}
            <div className="grid grid-cols-3 gap-6 pt-8 border-t border-orange-200">
              <div>
                <div className="text-orange-600">500+</div>
                <div className="text-sm text-gray-600">Verified Vendors</div>
              </div>
              <div>
                <div className="text-pink-600">2,000+</div>
                <div className="text-sm text-gray-600">Events Coordinated</div>
              </div>
              <div>
                <div className="text-purple-600">50+</div>
                <div className="text-sm text-gray-600">Cities Worldwide</div>
              </div>
            </div>
          </div>

          {/* Right Column - Visual */}
          <div className="relative">
            <div className="relative bg-white rounded-2xl shadow-2xl p-8 border border-orange-100">
              {/* Bundle Builder Demo */}
              <div className="space-y-6">
                <div className="flex items-center justify-between">
                  <h3 className="text-gray-900">Your Custom Bundle</h3>
                  <div className="px-3 py-1 bg-orange-100 text-orange-700 rounded-full text-sm">
                    Min. 2 Vendors
                  </div>
                </div>

                <div className="space-y-4">
                  {/* Selected Vendors */}
                  <div className="flex items-center gap-4 p-4 bg-gradient-to-r from-orange-50 to-pink-50 rounded-xl border border-orange-200">
                    <div className="w-12 h-12 bg-gradient-to-br from-orange-400 to-pink-400 rounded-lg flex items-center justify-center text-white">
                      <Calendar className="w-6 h-6" />
                    </div>
                    <div className="flex-1">
                      <div className="text-sm text-gray-600">DJ & Sound</div>
                      <div className="text-gray-900">Beats & Bhangra</div>
                    </div>
                    <div className="text-sm text-orange-600">$1,500</div>
                  </div>

                  <div className="flex items-center gap-4 p-4 bg-gradient-to-r from-purple-50 to-pink-50 rounded-xl border border-purple-200">
                    <div className="w-12 h-12 bg-gradient-to-br from-purple-400 to-pink-400 rounded-lg flex items-center justify-center text-white">
                      <Users className="w-6 h-6" />
                    </div>
                    <div className="flex-1">
                      <div className="text-sm text-gray-600">Catering</div>
                      <div className="text-gray-900">Spice & Soul</div>
                    </div>
                    <div className="text-sm text-purple-600">$3,200</div>
                  </div>

                  {/* Add More */}
                  <button className="w-full p-4 border-2 border-dashed border-gray-300 rounded-xl text-gray-600 hover:border-orange-300 hover:text-orange-600 transition-all flex items-center justify-center gap-2">
                    <span className="text-2xl">+</span>
                    <span>Add More Vendors</span>
                  </button>

                  {/* Bundle Summary */}
                  <div className="p-4 bg-gradient-to-br from-green-50 to-emerald-50 rounded-xl border border-green-200">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-sm text-gray-600">Bundle Total</span>
                      <span className="text-gray-900">$4,700</span>
                    </div>
                    <div className="flex items-center justify-between text-xs text-gray-600">
                      <span>2 vendors selected</span>
                      <span className="text-green-600">✓ Ready to book</span>
                    </div>
                  </div>
                </div>

                <button className="w-full py-3 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-xl hover:shadow-lg transition-all">
                  Review & Book Bundle
                </button>
              </div>
            </div>

            {/* Floating Badges */}
            <div className="absolute -top-4 -right-4 px-4 py-2 bg-white rounded-full shadow-lg border border-orange-200 animate-bounce">
              <span className="text-sm">✨ Build Your Own</span>
            </div>
            <div className="absolute -bottom-4 -left-4 px-4 py-2 bg-white rounded-full shadow-lg border border-pink-200">
              <span className="text-sm">🎉 Flexible</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}