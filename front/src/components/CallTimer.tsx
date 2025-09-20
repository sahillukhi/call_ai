import React, { useState, useEffect } from 'react';

interface CallTimerProps {
  startTime: number;
  isActive: boolean;
}

const CallTimer: React.FC<CallTimerProps> = ({ startTime, isActive }) => {
  const [duration, setDuration] = useState('00:00');

  useEffect(() => {
    if (!isActive) return;

    const updateTimer = () => {
      const now = Date.now();
      const elapsed = Math.floor((now - startTime) / 1000);
      
      const minutes = Math.floor(elapsed / 60);
      const seconds = elapsed % 60;
      
      setDuration(
        `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
      );
    };

    updateTimer(); // Initial update
    const interval = setInterval(updateTimer, 1000);

    return () => clearInterval(interval);
  }, [startTime, isActive]);

  if (!isActive) return null;

  return (
    <div className="call-timer animate-slide-in">
      {duration}
    </div>
  );
};

export default CallTimer;