import { Send, Sparkles, Calendar, Users, MapPin, DollarSign, MessageCircle, CheckCircle, Zap } from 'lucide-react';
import { useState } from 'react';
import type { Vendor } from '../App';

interface AIEventPlannerProps {
  onGenerateBundle: (vendors: Vendor[]) => void;
}

export function AIEventPlanner({ onGenerateBundle }: AIEventPlannerProps) {
  const [messages, setMessages] = useState([
    {
      type: 'ai',
      content: "Hi! I'm your AI Event Planning Assistant. I'll help you find the perfect vendor combination for your South Asian celebration. Just describe your event in your own words, and I'll dynamically match you with the best vendors based on availability, location, pricing, and service fit.",
      timestamp: new Date()
    },
    {
      type: 'ai',
      content: "For example, you can say: \"I'm planning a Sangeet for 150 guests in San Francisco on January 15th with a budget of $10,000\" or just start with \"I need help planning a wedding reception\"",
      timestamp: new Date()
    }
  ]);
  const [input, setInput] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [eventDetails, setEventDetails] = useState({
    eventType: '',
    guestCount: '',
    location: '',
    budget: '',
    date: ''
  });

  // Mock vendors for dynamic matching
  const allVendors: Vendor[] = [
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
      id: '4',
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
    }
  ];

  const quickPrompts = [
    "Sangeet for 150 guests in SF, $10k budget",
    "Wedding reception, 300 guests, need full coordination",
    "Engagement party for 75 people in Oakland",
    "Corporate Diwali event for 200 attendees"
  ];

  const handleSend = (text?: string) => {
    const messageText = text || input;
    if (!messageText.trim()) return;

    // Add user message
    const userMessage = {
      type: 'user' as const,
      content: messageText,
      timestamp: new Date()
    };
    setMessages(prev => [...prev, userMessage]);
    setInput('');
    setIsProcessing(true);

    // Simulate AI processing
    setTimeout(() => {
      // Extract details from message (simplified - in real app would use NLP)
      const hasEventType = messageText.toLowerCase().includes('sangeet') || 
                          messageText.toLowerCase().includes('wedding') ||
                          messageText.toLowerCase().includes('engagement');
      const hasLocation = messageText.toLowerCase().includes('san francisco') || 
                         messageText.toLowerCase().includes('sf') ||
                         messageText.toLowerCase().includes('oakland');
      const hasBudget = /\$?\d+[k]?/i.test(messageText);
      const hasGuests = /\d+\s*guests?/i.test(messageText);

      let aiResponse = "Let me analyze your requirements and match you with the perfect vendors...";
      
      if (hasEventType && hasLocation && (hasBudget || hasGuests)) {
        // Generate complete bundle
        aiResponse = "Perfect! I have all the information I need. I'm now searching our network for vendors that match your:";
        
        const analysisMessage = {
          type: 'ai' as const,
          content: aiResponse,
          timestamp: new Date()
        };
        setMessages(prev => [...prev, analysisMessage]);

        // Show matching criteria
        setTimeout(() => {
          const criteriaMessage = {
            type: 'criteria' as const,
            content: 'criteria',
            timestamp: new Date()
          };
          setMessages(prev => [...prev, criteriaMessage]);
          
          // Then show results
          setTimeout(() => {
            const recommendationMessage = {
              type: 'recommendation' as const,
              content: 'recommendation',
              timestamp: new Date()
            };
            setMessages(prev => [...prev, recommendationMessage]);
            setIsProcessing(false);
          }, 2000);
        }, 1000);
        return;
      } else {
        // Ask follow-up questions
        if (!hasEventType) {
          aiResponse = "Great! What type of event are you planning? (Wedding, Sangeet, Engagement, Mehndi, Birthday, etc.)";
        } else if (!hasLocation) {
          aiResponse = "Wonderful! Where will your event take place? (City or region)";
        } else if (!hasGuests) {
          aiResponse = "Perfect! How many guests are you expecting?";
        } else if (!hasBudget) {
          aiResponse = "Excellent! What's your approximate budget for vendor services?";
        }
      }

      const aiMessage = {
        type: 'ai' as const,
        content: aiResponse,
        timestamp: new Date()
      };
      setMessages(prev => [...prev, aiMessage]);
      setIsProcessing(false);
    }, 1000);
  };

  const handleAcceptBundle = () => {
    // Generate bundle with first 3 vendors
    onGenerateBundle(allVendors.slice(0, 3));
    
    const confirmMessage = {
      type: 'ai' as const,
      content: "Perfect! I've added these 3 vendors to your bundle. You can view and manage them in your cart, or continue customizing by adding more vendors or using the browse feature.",
      timestamp: new Date()
    };
    setMessages(prev => [...prev, confirmMessage]);
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-orange-50 to-pink-50 pb-20">
      <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
        {/* Header */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center gap-2 px-4 py-2 bg-white rounded-full border border-orange-200 mb-6">
            <Sparkles className="w-4 h-4 text-orange-500" />
            <span className="text-sm text-gray-700">AI-Powered Dynamic Matching</span>
          </div>
          <h1 className="mb-4 bg-gradient-to-r from-orange-600 to-pink-600 bg-clip-text text-transparent">
            Describe Your Event, We'll Build Your Bundle
          </h1>
          <p className="text-gray-600 max-w-2xl mx-auto">
            Just tell me about your event in natural language. I'll analyze your needs and dynamically 
            match you with vendors based on availability, proximity, pricing, and service compatibility.
          </p>
        </div>

        {/* Chat Interface */}
        <div className="bg-white rounded-2xl shadow-xl overflow-hidden border border-gray-200">
          {/* Messages */}
          <div className="h-[500px] overflow-y-auto p-6 space-y-4 bg-gradient-to-b from-white to-orange-50/30">
            {messages.map((message, index) => (
              <div key={index}>
                {message.type === 'ai' && (
                  <div className="flex gap-3 items-start">
                    <div className="w-10 h-10 bg-gradient-to-br from-orange-500 to-pink-500 rounded-full flex items-center justify-center flex-shrink-0">
                      <Sparkles className="w-5 h-5 text-white" />
                    </div>
                    <div className="flex-1">
                      <div className="bg-white p-4 rounded-2xl rounded-tl-none shadow-md border border-gray-100">
                        <p className="text-gray-700">{message.content}</p>
                      </div>
                      <div className="text-xs text-gray-500 mt-1 ml-2">
                        AI Assistant • Just now
                      </div>
                    </div>
                  </div>
                )}
                
                {message.type === 'user' && (
                  <div className="flex gap-3 items-start justify-end">
                    <div className="flex-1">
                      <div className="bg-gradient-to-r from-orange-500 to-pink-500 text-white p-4 rounded-2xl rounded-tr-none shadow-md ml-auto max-w-md">
                        <p>{message.content}</p>
                      </div>
                      <div className="text-xs text-gray-500 mt-1 mr-2 text-right">
                        You • Just now
                      </div>
                    </div>
                    <div className="w-10 h-10 bg-gradient-to-br from-gray-400 to-gray-500 rounded-full flex items-center justify-center flex-shrink-0">
                      <MessageCircle className="w-5 h-5 text-white" />
                    </div>
                  </div>
                )}

                {message.type === 'criteria' && (
                  <div className="my-4">
                    <div className="bg-gradient-to-br from-blue-50 to-purple-50 p-6 rounded-2xl border-2 border-blue-200">
                      <div className="flex items-center gap-2 mb-4">
                        <Zap className="w-6 h-6 text-blue-500" />
                        <h3 className="text-gray-900">Matching Criteria</h3>
                      </div>
                      
                      <div className="grid grid-cols-2 gap-4">
                        <div className="p-3 bg-white rounded-xl border border-blue-200">
                          <div className="flex items-center gap-2 mb-2">
                            <Calendar className="w-4 h-4 text-blue-500" />
                            <span className="text-sm text-gray-600">Availability</span>
                          </div>
                          <div className="text-sm text-gray-900">January 15, 2025</div>
                        </div>
                        <div className="p-3 bg-white rounded-xl border border-blue-200">
                          <div className="flex items-center gap-2 mb-2">
                            <MapPin className="w-4 h-4 text-blue-500" />
                            <span className="text-sm text-gray-600">Location</span>
                          </div>
                          <div className="text-sm text-gray-900">San Francisco</div>
                        </div>
                        <div className="p-3 bg-white rounded-xl border border-blue-200">
                          <div className="flex items-center gap-2 mb-2">
                            <Users className="w-4 h-4 text-blue-500" />
                            <span className="text-sm text-gray-600">Guest Count</span>
                          </div>
                          <div className="text-sm text-gray-900">~150 guests</div>
                        </div>
                        <div className="p-3 bg-white rounded-xl border border-blue-200">
                          <div className="flex items-center gap-2 mb-2">
                            <DollarSign className="w-4 h-4 text-blue-500" />
                            <span className="text-sm text-gray-600">Budget Range</span>
                          </div>
                          <div className="text-sm text-gray-900">~$10,000</div>
                        </div>
                      </div>

                      <div className="mt-4 p-3 bg-white rounded-xl border border-blue-200">
                        <div className="text-sm text-gray-700">
                          <strong>Matching Strategy:</strong> Prioritizing vendors with confirmed availability 
                          on your date, located within 20 miles of San Francisco, and with combined pricing 
                          near your budget range.
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {message.type === 'recommendation' && (
                  <div className="my-6">
                    <div className="bg-gradient-to-br from-green-50 to-emerald-50 p-6 rounded-2xl border-2 border-green-200">
                      <div className="flex items-center gap-2 mb-4">
                        <CheckCircle className="w-6 h-6 text-green-500" />
                        <h3 className="text-gray-900">Perfect Match Found!</h3>
                      </div>
                      
                      <p className="text-gray-700 mb-6">
                        Based on your requirements, I've dynamically assembled a custom bundle with 3 vendors 
                        that have <strong>confirmed availability</strong> on January 15th and are all located 
                        in San Francisco:
                      </p>

                      <div className="space-y-4">
                        {/* Vendor 1 */}
                        <div className="bg-white p-4 rounded-xl border border-green-200">
                          <div className="flex items-start gap-4">
                            <div className="w-12 h-12 bg-gradient-to-br from-purple-400 to-pink-400 rounded-lg flex items-center justify-center text-white text-xl flex-shrink-0">
                              🎧
                            </div>
                            <div className="flex-1">
                              <div className="flex items-center justify-between mb-2">
                                <h4 className="text-gray-900">Beats & Bhangra DJ Services</h4>
                                <span className="text-orange-600">$1,500</span>
                              </div>
                              <div className="text-sm text-gray-600 mb-2">DJ & Music • 4.9 ⭐ (156 reviews)</div>
                              <div className="flex flex-wrap gap-2">
                                <span className="px-2 py-1 bg-green-100 text-green-700 text-xs rounded">✓ Available Jan 15</span>
                                <span className="px-2 py-1 bg-blue-100 text-blue-700 text-xs rounded">San Francisco</span>
                                <span className="px-2 py-1 bg-purple-100 text-purple-700 text-xs rounded">Sangeet Specialist</span>
                              </div>
                            </div>
                          </div>
                        </div>

                        {/* Vendor 2 */}
                        <div className="bg-white p-4 rounded-xl border border-green-200">
                          <div className="flex items-start gap-4">
                            <div className="w-12 h-12 bg-gradient-to-br from-orange-400 to-red-400 rounded-lg flex items-center justify-center text-white text-xl flex-shrink-0">
                              🍛
                            </div>
                            <div className="flex-1">
                              <div className="flex items-center justify-between mb-2">
                                <h4 className="text-gray-900">Spice & Soul Catering</h4>
                                <span className="text-orange-600">$3,200</span>
                              </div>
                              <div className="text-sm text-gray-600 mb-2">Catering • 5.0 ⭐ (203 reviews)</div>
                              <div className="flex flex-wrap gap-2">
                                <span className="px-2 py-1 bg-green-100 text-green-700 text-xs rounded">✓ Available Jan 15</span>
                                <span className="px-2 py-1 bg-blue-100 text-blue-700 text-xs rounded">San Francisco</span>
                                <span className="px-2 py-1 bg-purple-100 text-purple-700 text-xs rounded">Fusion Menu</span>
                              </div>
                            </div>
                          </div>
                        </div>

                        {/* Vendor 3 */}
                        <div className="bg-white p-4 rounded-xl border border-green-200">
                          <div className="flex items-start gap-4">
                            <div className="w-12 h-12 bg-gradient-to-br from-blue-400 to-cyan-400 rounded-lg flex items-center justify-center text-white text-xl flex-shrink-0">
                              📸
                            </div>
                            <div className="flex-1">
                              <div className="flex items-center justify-between mb-2">
                                <h4 className="text-gray-900">Moments in Motion Photography</h4>
                                <span className="text-orange-600">$2,800</span>
                              </div>
                              <div className="text-sm text-gray-600 mb-2">Photography • 4.9 ⭐ (187 reviews)</div>
                              <div className="flex flex-wrap gap-2">
                                <span className="px-2 py-1 bg-green-100 text-green-700 text-xs rounded">✓ Available Jan 15</span>
                                <span className="px-2 py-1 bg-blue-100 text-blue-700 text-xs rounded">San Francisco</span>
                                <span className="px-2 py-1 bg-purple-100 text-purple-700 text-xs rounded">Cinematic Style</span>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>

                      {/* Bundle Summary */}
                      <div className="mt-6 p-4 bg-white rounded-xl border-2 border-green-300">
                        <div className="grid grid-cols-2 gap-4 mb-4">
                          <div>
                            <div className="text-sm text-gray-600 mb-1">Bundle Total</div>
                            <div className="text-gray-900">$7,500</div>
                          </div>
                          <div>
                            <div className="text-sm text-gray-600 mb-1">Budget Match</div>
                            <div className="text-green-600">Within range ✓</div>
                          </div>
                          <div>
                            <div className="text-sm text-gray-600 mb-1">Vendors</div>
                            <div className="text-gray-900">3 coordinated</div>
                          </div>
                          <div>
                            <div className="text-sm text-gray-600 mb-1">Availability</div>
                            <div className="text-green-600">All confirmed ✓</div>
                          </div>
                        </div>
                        
                        <div className="text-xs text-gray-600">
                          💡 <strong>Why this bundle?</strong> These vendors frequently work together, 
                          are all verified for South Asian events, and have excellent ratings from similar celebrations.
                        </div>
                      </div>

                      {/* CTA */}
                      <div className="mt-6 flex gap-3">
                        <button 
                          onClick={handleAcceptBundle}
                          className="flex-1 px-6 py-3 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-xl hover:shadow-lg transition-all"
                        >
                          Add Bundle to Cart
                        </button>
                        <button className="px-6 py-3 border-2 border-gray-300 text-gray-700 rounded-xl hover:bg-gray-50 transition-all">
                          Customize
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            ))}

            {isProcessing && (
              <div className="flex gap-3 items-start">
                <div className="w-10 h-10 bg-gradient-to-br from-orange-500 to-pink-500 rounded-full flex items-center justify-center flex-shrink-0">
                  <Sparkles className="w-5 h-5 text-white animate-pulse" />
                </div>
                <div className="flex-1">
                  <div className="bg-white p-4 rounded-2xl rounded-tl-none shadow-md border border-gray-100">
                    <div className="flex items-center gap-2 text-gray-500">
                      <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }}></div>
                      <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }}></div>
                      <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }}></div>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Quick Prompts */}
          <div className="px-6 py-4 bg-gradient-to-r from-orange-50 to-pink-50 border-t border-orange-100">
            <div className="text-xs text-gray-600 mb-2">Try these examples:</div>
            <div className="flex flex-wrap gap-2">
              {quickPrompts.map((prompt, index) => (
                <button
                  key={index}
                  onClick={() => handleSend(prompt)}
                  className="px-4 py-2 bg-white border border-orange-200 rounded-full text-sm text-gray-700 hover:bg-orange-100 hover:border-orange-300 transition-all"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>

          {/* Input */}
          <div className="p-4 border-t border-gray-200 bg-white">
            <div className="flex gap-3">
              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyPress={(e) => e.key === 'Enter' && !isProcessing && handleSend()}
                placeholder="Describe your event in natural language..."
                className="flex-1 px-4 py-3 border border-gray-300 rounded-xl focus:outline-none focus:ring-2 focus:ring-orange-500 focus:border-transparent"
                disabled={isProcessing}
              />
              <button
                onClick={() => handleSend()}
                disabled={isProcessing}
                className="px-6 py-3 bg-gradient-to-r from-orange-500 to-pink-500 text-white rounded-xl hover:shadow-lg transition-all flex items-center gap-2 disabled:opacity-50"
              >
                <Send className="w-5 h-5" />
              </button>
            </div>
          </div>
        </div>

        {/* Features */}
        <div className="grid md:grid-cols-3 gap-6 mt-8">
          <div className="p-6 bg-white rounded-xl border border-orange-100 text-center">
            <div className="w-12 h-12 bg-gradient-to-br from-orange-500 to-pink-500 rounded-full flex items-center justify-center mx-auto mb-4">
              <Sparkles className="w-6 h-6 text-white" />
            </div>
            <h4 className="mb-2 text-gray-900">Natural Language</h4>
            <p className="text-sm text-gray-600">
              Just describe your event—no forms or complicated inputs needed
            </p>
          </div>
          <div className="p-6 bg-white rounded-xl border border-orange-100 text-center">
            <div className="w-12 h-12 bg-gradient-to-br from-purple-500 to-pink-500 rounded-full flex items-center justify-center mx-auto mb-4">
              <Zap className="w-6 h-6 text-white" />
            </div>
            <h4 className="mb-2 text-gray-900">Dynamic Matching</h4>
            <p className="text-sm text-gray-600">
              AI analyzes availability, proximity, and compatibility in real-time
            </p>
          </div>
          <div className="p-6 bg-white rounded-xl border border-orange-100 text-center">
            <div className="w-12 h-12 bg-gradient-to-br from-green-500 to-emerald-500 rounded-full flex items-center justify-center mx-auto mb-4">
              <CheckCircle className="w-6 h-6 text-white" />
            </div>
            <h4 className="mb-2 text-gray-900">Optimized Bundles</h4>
            <p className="text-sm text-gray-600">
              Get 3+ coordinated vendors that work well together
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
