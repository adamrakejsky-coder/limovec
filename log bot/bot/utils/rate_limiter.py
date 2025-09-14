import time
from typing import Dict, List, Union, Hashable


class RateLimiter:
    def __init__(self, max_calls: int = 5, window: int = 60):
        self.max_calls = max_calls
        self.window = window
        self.calls: Dict[Hashable, List[float]] = {}
    
    def can_call(self, key: Union[int, str]) -> bool:
        """Zkontroluje zda může uživatel/klíč provést akci"""
        current_time = time.time()
        
        if key not in self.calls:
            self.calls[key] = []
        
        # Odstraň staré volání
        self.calls[key] = [
            call_time for call_time in self.calls[key] 
            if current_time - call_time < self.window
        ]
        
        if len(self.calls[key]) < self.max_calls:
            self.calls[key].append(current_time)
            return True
        
        return False
    
    def get_cooldown(self, key: Union[int, str]) -> int:
        """Vrátí kolik sekund musí uživatel čekat"""
        if key not in self.calls or not self.calls[key]:
            return 0
        
        oldest_call = min(self.calls[key])
        return max(0, int(self.window - (time.time() - oldest_call)))