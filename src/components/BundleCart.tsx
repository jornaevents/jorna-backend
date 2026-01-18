import { X, Trash2, AlertCircle, CheckCircle, Calendar, ArrowRight } from 'lucide-react';
import type { Vendor } from '../App';

interface BundleCartProps {
  isOpen: boolean;
  onClose: () => void;
  vendors: Vendor[];
  onRemoveVendor: (vendorId: string) => void;
  onClearBundle: () => void;
}

export function BundleCart({ isOpen, onClose, vendors, onRemoveVendor, onClearBundle }: BundleCartProps) {
  const totalPrice = vendors.reduce((sum, vendor) => sum + vendor.price, 0);
  const canBook = vendors.length >= 2;
  const needsMoreVendors = 2 - vendors.length;

  // Check common availability across vendors
  const commonAvailability = vendors.length > 0 
    ? vendors[0].availability.filter(date => 
        vendors.every(vendor => vendor.availability.includes(date))
      )
    : [];

  if (!isOpen) return null;

  return (
    <>
      {/* Backdrop */}
      <div 
        className="fixed inset-0 bg-black/50 z-40"
        onClick={onClose}
      />

      {/* Sidebar */}
      <div className="fixed top-0 right-0 h-full w-full max-w-md bg-white shadow-2xl z-50 flex flex-col">
        {/* Header */}
        <div className="p-6 border-b border-gray-200 bg-gradient-to-r from-orange-50 to-pink-50">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-gray-900">Your Event Bundle</h2>
            <button
              onClick={onClose}
              className="p-2 hover:bg-white rounded-lg transition-colors"
            >
              <X className="w-6 h-6 text-gray-600" />
            </button>
          </div>

          {/* Status Banner */}
          <div className={`p-4 rounded-xl border-2 ${
            canBook 
              ? 'bg-green-50 border-green-200' 
              : 'bg-orange-50 border-orange-200'
          }`}>
            <div className="flex items-start gap-3">
              {canBook ? (
                <CheckCircle className="w-5 h-5 text-green-500 flex-shrink-0 mt-0.5" />
              ) : (
                <AlertCircle className="w-5 h-5 text-orange-500 flex-shrink-0 mt-0.5" />
              )}
              <div>
                <div className={`text-sm mb-1 ${canBook ? 'text-green-900' : 'text-orange-900'}`}>
                  {canBook ? (
                    <span>Ready to book!</span>
                  ) : (
                    <span>Add {needsMoreVendors} more vendor{needsMoreVendors > 1 ? 's' : ''}</span>
                  )}
                </div>
                <div className={`text-xs ${canBook ? 'text-green-700' : 'text-orange-700'}`}>
                  {canBook 
                    ? 'Your bundle meets the minimum requirement'
                    : 'Minimum 2 vendors required for coordination'
                  }
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Vendors List */}
        <div className="flex-1 overflow-y-auto p-6">
          {vendors.length === 0 ? (
            <div className="text-center py-12">
              <div className="text-6xl mb-4">🎉</div>
              <h3 className="mb-2 text-gray-900">No vendors yet</h3>
              <p className="text-sm text-gray-600">
                Start building your bundle by browsing vendors or using the AI planner
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              {vendors.map((vendor, index) => (
                <div 
                  key={vendor.id}
                  className="p-4 bg-gradient-to-br from-orange-50 to-pink-50 rounded-xl border border-orange-200"
                >
                  <div className="flex items-start gap-3 mb-3">
                    <div className="w-12 h-12 bg-gradient-to-br from-orange-400 to-pink-400 rounded-lg flex items-center justify-center text-white text-xl flex-shrink-0">
                      {vendor.category === 'DJ & Music' ? '🎧' :
                       vendor.category === 'Catering' ? '🍛' :
                       vendor.category === 'Decoration' ? '🌸' :
                       vendor.category === 'Photography' ? '📸' :
                       vendor.category === 'Mehndi' ? '🌺' : '📋'}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-gray-900 mb-1">{vendor.name}</div>
                      <div className="text-xs text-gray-600 mb-2">{vendor.category}</div>
                      <div className="flex items-center justify-between">
                        <div className="text-orange-600">${vendor.price.toLocaleString()}</div>
                        <button
                          onClick={() => onRemoveVendor(vendor.id)}
                          className="p-1 text-gray-500 hover:text-red-500 transition-colors"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              ))}

              {/* Common Availability */}
              {vendors.length >= 2 && (
                <div className="p-4 bg-blue-50 rounded-xl border border-blue-200">
                  <div className="flex items-start gap-2 mb-3">
                    <Calendar className="w-5 h-5 text-blue-500 flex-shrink-0 mt-0.5" />
                    <div>
                      <div className="text-sm text-blue-900 mb-1">
                        Common Availability
                      </div>
                      <div className="text-xs text-blue-700">
                        Dates when all vendors are available
                      </div>
                    </div>
                  </div>
                  {commonAvailability.length > 0 ? (
                    <div className="flex flex-wrap gap-2">
                      {commonAvailability.map((date, index) => (
                        <div
                          key={index}
                          className="px-3 py-1 bg-white border border-blue-200 rounded-full text-xs text-blue-700"
                        >
                          {new Date(date).toLocaleDateString('en-US', { 
                            month: 'short', 
                            day: 'numeric',
                            year: 'numeric'
                          })}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-xs text-blue-700">
                      No common availability found. You may need to coordinate separately.
                    </div>
                  )}
                </div>
              )}

              {/* Clear Button */}
              {vendors.length > 0 && (
                <button
                  onClick={onClearBundle}
                  className="w-full py-2 text-sm text-red-600 hover:text-red-700 transition-colors"
                >
                  Clear All Vendors
                </button>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        {vendors.length > 0 && (
          <div className="p-6 border-t border-gray-200 bg-gray-50">
            {/* Bundle Summary */}
            <div className="mb-6 space-y-3">
              <div className="flex items-center justify-between text-sm text-gray-600">
                <span>Vendors Selected</span>
                <span className="text-gray-900">{vendors.length}</span>
              </div>
              <div className="flex items-center justify-between text-sm text-gray-600">
                <span>Estimated Total</span>
                <span className="text-gray-900">${totalPrice.toLocaleString()}</span>
              </div>
              {vendors.length >= 3 && (
                <div className="p-3 bg-green-50 border border-green-200 rounded-lg">
                  <div className="flex items-center gap-2 text-sm text-green-700">
                    <CheckCircle className="w-4 h-4" />
                    <span>Complete coverage! 3+ vendors</span>
                  </div>
                </div>
              )}
            </div>

            {/* CTA Buttons */}
            <div className="space-y-3">
              {canBook ? (
                <>
                  <button className="w-full py-4 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-xl hover:shadow-lg transition-all flex items-center justify-center gap-2 group">
                    <span>Review & Book Bundle</span>
                    <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
                  </button>
                  <button 
                    onClick={onClose}
                    className="w-full py-3 border-2 border-gray-300 text-gray-700 rounded-xl hover:bg-gray-100 transition-all"
                  >
                    Continue Browsing
                  </button>
                </>
              ) : (
                <button 
                  onClick={onClose}
                  className="w-full py-4 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-xl hover:shadow-lg transition-all"
                >
                  Add More Vendors
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </>
  );
}
