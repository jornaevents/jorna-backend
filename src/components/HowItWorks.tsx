import { Search, PackageCheck, Calendar, PartyPopper } from 'lucide-react';

export function HowItWorks() {
  const steps = [
    {
      icon: Search,
      title: 'Browse or Describe',
      description: 'Manually browse vendors by category and build your bundle, or describe your event to our AI planner in natural language.',
      color: 'from-orange-500 to-orange-600'
    },
    {
      icon: PackageCheck,
      title: 'Build Your Bundle',
      description: 'Select at least 2 vendors (we recommend 3+) for complete coordination. The system checks availability, proximity, and compatibility.',
      color: 'from-pink-500 to-pink-600'
    },
    {
      icon: Calendar,
      title: 'Book in One Go',
      description: 'Review your custom bundle with consolidated pricing and scheduling. Complete the entire booking through a single checkout.',
      color: 'from-purple-500 to-purple-600'
    },
    {
      icon: PartyPopper,
      title: 'Celebrate Together',
      description: 'Your coordinated vendors arrive prepared and aligned. Focus on your celebration while we handle the logistics.',
      color: 'from-indigo-500 to-indigo-600'
    }
  ];

  return (
    <div className="py-20 bg-white">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="text-center mb-16">
          <h2 className="mb-4 bg-gradient-to-r from-orange-600 to-pink-600 bg-clip-text text-transparent">
            How DesiConnect Works
          </h2>
          <p className="text-gray-600 max-w-2xl mx-auto">
            Dynamic bundle creation puts you in control. Build your perfect vendor team 
            manually or let AI do the heavy lifting—all with guaranteed coordination.
          </p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-4 gap-8">
          {steps.map((step, index) => (
            <div key={index} className="relative group">
              {/* Connector Line */}
              {index < steps.length - 1 && (
                <div className="hidden lg:block absolute top-16 left-[calc(50%+2rem)] w-[calc(100%-2rem)] h-0.5 bg-gradient-to-r from-orange-200 to-pink-200 z-0"></div>
              )}

              <div className="relative bg-white p-6 rounded-2xl border border-gray-200 hover:border-orange-300 hover:shadow-xl transition-all z-10">
                {/* Step Number */}
                <div className="absolute -top-4 -left-4 w-8 h-8 bg-gradient-to-br from-orange-500 to-pink-500 text-white rounded-full flex items-center justify-center shadow-lg">
                  {index + 1}
                </div>

                {/* Icon */}
                <div className={`w-16 h-16 bg-gradient-to-br ${step.color} rounded-xl flex items-center justify-center mb-6 group-hover:scale-110 transition-transform`}>
                  <step.icon className="w-8 h-8 text-white" />
                </div>

                {/* Content */}
                <h3 className="mb-3 text-gray-900">{step.title}</h3>
                <p className="text-sm text-gray-600 leading-relaxed">
                  {step.description}
                </p>
              </div>
            </div>
          ))}
        </div>

        {/* Bundle Requirements Callout */}
        <div className="mt-16 max-w-3xl mx-auto p-8 bg-gradient-to-br from-orange-50 to-pink-50 rounded-2xl border-2 border-orange-200">
          <div className="flex items-start gap-4">
            <div className="w-12 h-12 bg-gradient-to-br from-orange-500 to-pink-500 rounded-full flex items-center justify-center flex-shrink-0">
              <PackageCheck className="w-6 h-6 text-white" />
            </div>
            <div>
              <h3 className="mb-2 text-gray-900">Bundle Requirements</h3>
              <p className="text-gray-700 mb-4">
                Every bundle must include <strong>at least 2 vendors</strong> to ensure proper coordination. 
                We recommend <strong>3 or more vendors</strong> for complete event coverage—think DJ + Catering + 
                Photography, or Décor + Mehndi + Entertainment.
              </p>
              <p className="text-sm text-gray-600">
                The more services you coordinate through DesiConnect, the smoother your event runs. 
                Our system automatically checks vendor availability, location compatibility, and service alignment.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}