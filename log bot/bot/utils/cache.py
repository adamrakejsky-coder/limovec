from collections import OrderedDict
import time


class LRUCache:
    def __init__(self, max_size=1000):
        self.max_size = max_size
        self.cache = OrderedDict()
        self.expiry = {}
    
    def get(self, key, default=None):
        if key in self.cache:
            # Zkontroluj expiraci
            if key in self.expiry and time.time() > self.expiry[key]:
                del self.cache[key]
                del self.expiry[key]
                return default
            
            # Move to end (most recently used)
            self.cache.move_to_end(key)
            return self.cache[key]
        return default
    
    def set(self, key, value, expire_in=3600):  # 1 hodina default
        if key in self.cache:
            self.cache.move_to_end(key)
        else:
            if len(self.cache) >= self.max_size:
                # Remove oldest
                oldest = next(iter(self.cache))
                del self.cache[oldest]
                if oldest in self.expiry:
                    del self.expiry[oldest]
        
        self.cache[key] = value
        self.expiry[key] = time.time() + expire_in
    
    def cleanup_expired(self):
        """Odstraň expirované záznamy"""
        current_time = time.time()
        expired_keys = [k for k, exp_time in self.expiry.items() if current_time > exp_time]
        for key in expired_keys:
            if key in self.cache:
                del self.cache[key]
            del self.expiry[key]
        return len(expired_keys)