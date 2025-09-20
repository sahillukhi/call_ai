import React, { useState, useRef, useEffect } from 'react';
import { Phone } from 'lucide-react';

interface SlideToCallProps {
  onCallStart: () => void;
  disabled?: boolean;
}

const SlideToCall: React.FC<SlideToCallProps> = ({ onCallStart, disabled = false }) => {
  const [isDragging, setIsDragging] = useState(false);
  const [slidePosition, setSlidePosition] = useState(0);
  const [isComplete, setIsComplete] = useState(false);
  const sliderRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLDivElement>(null);
  const maxSlide = 258; // 320px - 62px (thumb width)

  const handleStart = (clientX: number) => {
    if (disabled || isComplete) return;
    setIsDragging(true);
  };

  const handleMove = (clientX: number) => {
    if (!isDragging || disabled || isComplete) return;
    
    if (sliderRef.current) {
      const rect = sliderRef.current.getBoundingClientRect();
      const newPosition = Math.max(0, Math.min(maxSlide, clientX - rect.left - 31));
      setSlidePosition(newPosition);
      
      // Check if slide is complete (90% of the way)
      if (newPosition > maxSlide * 0.9) {
        setIsComplete(true);
        setIsDragging(false);
        setTimeout(() => {
          onCallStart();
        }, 200);
      }
    }
  };

  const handleEnd = () => {
    if (disabled || isComplete) return;
    setIsDragging(false);
    
    // Reset position if not complete
    if (slidePosition <= maxSlide * 0.9) {
      setSlidePosition(0);
    }
  };

  // Mouse events
  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    handleStart(e.clientX);
  };

  const handleMouseMove = (e: MouseEvent) => {
    handleMove(e.clientX);
  };

  const handleMouseUp = () => {
    handleEnd();
  };

  // Touch events
  const handleTouchStart = (e: React.TouchEvent) => {
    e.preventDefault();
    handleStart(e.touches[0].clientX);
  };

  const handleTouchMove = (e: TouchEvent) => {
    e.preventDefault();
    handleMove(e.touches[0].clientX);
  };

  const handleTouchEnd = (e: TouchEvent) => {
    e.preventDefault();
    handleEnd();
  };

  useEffect(() => {
    if (isDragging) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);
      document.addEventListener('touchmove', handleTouchMove);
      document.addEventListener('touchend', handleTouchEnd);
    }

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
      document.removeEventListener('touchmove', handleTouchMove);
      document.removeEventListener('touchend', handleTouchEnd);
    };
  }, [isDragging]);

  const thumbStyle = {
    left: `${slidePosition}px`,
    transition: isDragging ? 'none' : 'left 0.3s ease-out'
  };

  const textOpacity = Math.max(0, 1 - (slidePosition / maxSlide) * 2);

  return (
    <div className={`slide-to-call mx-auto ${!disabled ? 'pulse-glow' : ''}`}>
      <div className="slide-track" ref={sliderRef}>
        <div
          className="slide-thumb"
          ref={thumbRef}
          style={thumbStyle}
          onMouseDown={handleMouseDown}
          onTouchStart={handleTouchStart}
        >
          <Phone size={24} />
        </div>
        <div 
          className="slide-text"
          style={{ opacity: textOpacity }}
        >
          {isComplete ? 'Connecting...' : 'Slide to Call'}
        </div>
      </div>
    </div>
  );
};

export default SlideToCall;