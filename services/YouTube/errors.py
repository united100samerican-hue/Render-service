class SocialMediaError(RuntimeError):
    def __init__(self,code:str,detail:str=''):
        self.code,self.detail=code,detail
        super().__init__(detail or code)
