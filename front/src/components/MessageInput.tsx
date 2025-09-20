import React, { useState } from 'react';
import { Input } from '@/components/ui/input';
import { Send } from 'lucide-react';

interface MessageInputProps {
  onSendMessage: (message: string) => void;
  placeholder?: string;
  disabled?: boolean;
}

const MessageInput: React.FC<MessageInputProps> = ({ 
  onSendMessage, 
  placeholder = "Type a message...",
  disabled = false 
}) => {
  const [message, setMessage] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (message.trim() && !disabled) {
      onSendMessage(message.trim());
      setMessage('');
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="w-full max-w-md mx-auto">
      <div className="relative">
        <Input
          type="text"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyPress={handleKeyPress}
          placeholder={placeholder}
          disabled={disabled}
          className="pr-12 bg-card/50 backdrop-blur-sm border-border/50 text-foreground placeholder:text-muted-foreground focus:ring-primary/50 focus:border-primary/50"
        />
        {message.trim() && (
          <button
            type="submit"
            disabled={disabled}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-2 text-primary hover:text-primary-glow transition-colors"
          >
            <Send size={16} />
          </button>
        )}
      </div>
    </form>
  );
};

export default MessageInput;