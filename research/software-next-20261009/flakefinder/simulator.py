"""Deterministic microscope simulator with a missing chip and a tilted focal plane."""
import threading
import cv2, numpy as np
from .model import Position, Frame, SafetyError, Cancelled

class SimulatedMicroscope:
    def __init__(self,seed=7):
        self._position=Position(250,250,100)
        self.cancel=threading.Event()
        self.limits=None; self.armed=False; self.expected_scale=None; self.sequence=0
        self.pixel_size=1.0
        self.shape=(384,512)
        self.rng=np.random.default_rng(seed)
        self.world=np.full((2300,2300,3),30,dtype=np.float32)
        self.truth=[]
        for r in range(4):
            for c in range(4):
                if (r,c)==(2,3): continue
                x,y=30+c*560,30+r*560
                substrate=np.array([175,145,190],dtype=np.float32)
                self.world[y:y+480,x:x+480]=substrate
                for n in range(16):
                    px,py=x+int(self.rng.integers(25,440)),y+int(self.rng.integers(25,440))
                    w,h=self.rng.integers(6,24,2)
                    contrast=np.array([0.04,0.075,0.025])*(1+n%4)
                    contour=np.array([[px,py],[px+w,py+3],[px+w-3,py+h],[px+2,py+h-1]],np.int32)
                    cv2.fillPoly(self.world,[contour],(substrate*(1-contrast)).tolist())
                    self.truth.append({'chip':f'{chr(65+r)}{c+1}','xy':[px+w/2,py+h/2],'layers':1+n%4})
        self.world+=self.rng.normal(0,0.6,self.world.shape).astype(np.float32)

    def position(self): return self._position
    def arm(self,limits):
        limits.check(self.position()); self.limits=limits; self.cancel.clear(); self.armed=True
    def check_cancel(self):
        if self.cancel.is_set(): raise Cancelled('Simulator stopped.')
    def move(self,p):
        self.check_cancel()
        if not self.armed: raise SafetyError('Motion is not armed.')
        self.limits.check_move(self.position(),p); self._position=p
        return p
    def move_z(self,z):
        p=self.position(); return self.move(Position(p.x,p.y,float(z)))
    def travel(self,x,y):
        p=self.position()
        if not self.armed: raise SafetyError('Motion is not armed.')
        self.limits.check(Position(x,y,p.z))
        n=max(1,int(np.ceil(np.hypot(x-p.x,y-p.y)/self.limits.max_xy_step_um)))
        for i in range(1,n+1): self.move(Position(p.x+(x-p.x)*i/n,p.y+(y-p.y)*i/n,p.z))
    def capture(self):
        self.check_cancel(); p=self.position(); self.sequence+=1
        if self.expected_scale is not None and not np.allclose(self.expected_scale,(self.pixel_size,self.pixel_size),rtol=.01):
            raise SafetyError('Simulator camera scale changed during acquisition.')
        h,w=self.shape
        xx,yy=np.meshgrid(np.arange(w,dtype=np.float32)-(w-1)/2,np.arange(h,dtype=np.float32)-(h-1)/2)
        rgb=cv2.remap(self.world,xx*self.pixel_size+p.x,yy*self.pixel_size+p.y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT,borderValue=(30,30,30))
        focus=100+0.003*(p.x-250)-0.002*(p.y-250)
        blur=abs(p.z-focus)*0.35
        if blur>0.05: rgb=cv2.GaussianBlur(rgb,(0,0),blur)
        illumination=1-0.08*(xx/(w/2))**2-0.04*(yy/(h/2))**2
        rgb=np.uint16(np.clip((rgb*illumination[:,:,None]+self.rng.normal(0,0.25,rgb.shape))*16,0,4095))
        return Frame(rgb,p,(self.pixel_size,self.pixel_size),self.sequence,metadata={'source':'SIMULATION','nominal_max':4095})
    def stop(self): self.cancel.set(); self.armed=False; return []
    def close(self): self.armed=False
    def set_objective(self,*_): raise SafetyError('Simulator uses a fixed 1 um/pixel camera.')
