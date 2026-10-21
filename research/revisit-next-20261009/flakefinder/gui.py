"""Local desktop console. A single worker owns acquisition; Stop is independent."""
from pathlib import Path
from dataclasses import asdict
import copy,json,queue,threading,time,traceback
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
import cv2,numpy as np
from PIL import Image,ImageTk
from .config import load_config,connect,arm,save_transform,transform_from_config
from .model import SafetyError,AcquisitionError,CameraTransform,atomic_json
from .imaging import preview,save_frame,load_frame


def configuration_for_objective_profile(config,profile,objective_state):
    """Prepare a measured lens profile without moving hardware or reusing old lens data."""
    if config.get('backend')!='zen':raise SafetyError('Objective profiles apply to the connected ZEISS microscope.')
    if profile.get('schema')!=1:raise ValueError('Expected an objective profile with schema 1.')
    objective=profile['objective'];position=objective['position']
    actual=objective_state.get('position')
    if actual!=position or not actual:
        raise SafetyError(f'Manually engage {objective["name"]} at position {position}; encoded readback is {actual}.')
    configured=next((item for item in objective_state['objectives'] if item['position']==position),None)
    if configured is None or configured['name']!=objective['name'] or configured['magnification']!=objective['magnification']:
        raise SafetyError('Objective profile does not match the connected objective catalog.')
    transform=CameraTransform(**profile['camera_transform'])
    if not transform.calibrated:raise SafetyError('This objective profile has no measured camera transform.')
    sample=config.get('context',{}).get('sample_id')
    if not sample or profile.get('context',{}).get('sample_id')!=sample:
        raise SafetyError('Objective profile belongs to another sample; measure/save a profile for this sample.')
    if not profile.get('job_template') or not profile.get('camera_resources'):
        raise ValueError('Objective profile is missing its fixed camera acquisition settings.')
    if not 0<float(profile['illumination_percent'])<=100:raise ValueError('Objective profile has invalid illumination.')
    if not profile.get('focus') or profile['focus']['half_range_um']<=0:raise ValueError('Objective profile has no usable focus settings.')
    cfg=copy.deepcopy(config)
    previous=cfg.get('zen',{}).get('expected_objective_position')
    cfg['zen'].update(job_template=profile['job_template'],camera_resources=copy.deepcopy(profile['camera_resources']),expected_objective_position=position)
    cfg['camera_transform']=copy.deepcopy(profile['camera_transform'])
    cfg['context']=copy.deepcopy(profile['context'])
    cfg['context'].update(objective=objective['name'],illumination_percent=profile['illumination_percent'])
    if 'ff_exposure_ms' in profile['camera_resources']:cfg['context']['exposure_ms']=profile['camera_resources']['ff_exposure_ms']
    if 'ff_white_balance' in profile['camera_resources']:cfg['context']['white_balance']=profile['camera_resources']['ff_white_balance']
    cfg['focus']=copy.deepcopy(profile['focus'])
    cfg['material_model']=None  # A material catalog is tied to its optical acquisition settings.
    cfg.pop('calibration_quality',None)
    if profile.get('calibration_quality'):cfg['calibration_quality']=copy.deepcopy(profile['calibration_quality'])
    if previous!=position:cfg['initial_focus']=None  # A historical absolute Z range belongs to the old lens.
    return cfg


def describe_optics(config,state=None,lamp=None):
    """Concise display text; unknown readback is never presented as the chosen lens."""
    expected=config.get('zen',{}).get('expected_objective_position')
    resources=config.get('zen',{}).get('camera_resources',{})
    exposure=resources.get('ff_exposure_ms')
    setting=f'{exposure:.1f} ms' if isinstance(exposure,(float,int)) else 'exposure from job'
    if state is None:return f'Configured: {config.get("context",{}).get("objective","objective unspecified")} · {setting} · readback pending'
    actual=state.get('position')
    objective=next((o for o in state.get('objectives',[]) if o['position']==actual),None)
    lens=objective['name'] if objective else 'NO ENGAGED OBJECTIVE — seat the turret in its click stop'
    mismatch=f' · profile expects position {expected}' if expected and expected!=actual else ''
    light=f' · LED {lamp:.1f}%' if lamp is not None and 0<=lamp<=100 else ' · illumination unavailable'
    return f'{lens}{mismatch} · {setting}{light}'

class App:
    def __init__(self,root,config_path):
        self.root=root;self.scope=None;self.config=None;self.busy=False;self.events=queue.Queue()
        self.last_frame=None;self.flakes=[];self.results_folder=None;self.closing=False
        self.stopping=False;self.active_profile=None;self.loaded_config_path=None
        self.review_servers=[]
        root.title('FlakeFinder · microscope and chip scanning');root.geometry('1340x870')
        root.minsize(1040,720);root.protocol('WM_DELETE_WINDOW',self.close)
        self.config_path=tk.StringVar(value=str(Path(config_path).resolve()))
        from .storage import resolve_output_path
        self.output=tk.StringVar(value=str(resolve_output_path(Path('data/session-'+time.strftime('%Y%m%d-%H%M%S')),verify=False)))
        self.status=tk.StringVar(value='Disconnected · motion disabled');self.position=tk.StringVar(value='XYZ —')
        self.optical_status=tk.StringVar(value='Objective readback pending · objective changes are manual')
        default_profile=Path(config_path).resolve().parent/'objective-20x-quantitative.json'
        self.profile_path=tk.StringVar(value=str(default_profile) if default_profile.exists() else '')
        self.revisit_selection=tk.StringVar(value='Kept in gallery');self.revisit_limit=tk.StringVar(value='12')
        self.resume=tk.BooleanVar(value=False)
        top=ttk.Frame(root,padding=12);top.pack(fill='x')
        ttk.Label(top,text='FLAKEFINDER',font=('Segoe UI',18,'bold')).pack(side='left')
        tk.Button(top,text='STOP / DISARM',bg='#ab2036',fg='white',font=('Segoe UI',11,'bold'),command=self.stop).pack(side='right',padx=8,ipady=4)
        ttk.Label(top,textvariable=self.position).pack(side='right')
        strip=ttk.Frame(root,padding=(12,0));strip.pack(fill='x')
        ttk.Label(strip,text='Configuration').grid(row=0,column=0,sticky='w')
        ttk.Entry(strip,textvariable=self.config_path,width=90).grid(row=0,column=1,sticky='ew',padx=8)
        ttk.Button(strip,text='Browse',command=self.choose_config).grid(row=0,column=2)
        ttk.Label(strip,text='Session folder').grid(row=1,column=0,sticky='w',pady=6)
        ttk.Entry(strip,textvariable=self.output).grid(row=1,column=1,sticky='ew',padx=8)
        ttk.Button(strip,text='Browse',command=self.choose_output).grid(row=1,column=2)
        ttk.Label(strip,text='Objective profile').grid(row=2,column=0,sticky='w')
        ttk.Entry(strip,textvariable=self.profile_path,state='readonly').grid(row=2,column=1,sticky='ew',padx=8)
        ttk.Button(strip,text='Select / apply',command=self.choose_profile).grid(row=2,column=2)
        ttk.Label(strip,textvariable=self.optical_status,wraplength=1150).grid(row=3,column=0,columnspan=3,sticky='w',pady=(5,0))
        strip.columnconfigure(1,weight=1)
        body=ttk.Panedwindow(root,orient='horizontal');body.pack(fill='both',expand=True,padx=12,pady=8)
        scroll_panel=ttk.Frame(body,width=280);body.add(scroll_panel,weight=0)
        control_canvas=tk.Canvas(scroll_panel,width=270,highlightthickness=0)
        scrollbar=ttk.Scrollbar(scroll_panel,orient='vertical',command=control_canvas.yview)
        control_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y');control_canvas.pack(side='left',fill='both',expand=True)
        controls=ttk.Frame(control_canvas,width=250,padding=6)
        control_canvas.create_window((0,0),window=controls,anchor='nw')
        controls.bind('<Configure>',lambda _:control_canvas.configure(scrollregion=control_canvas.bbox('all')))
        self.buttons=[]
        groups=[('MICROSCOPE',[('Connect / reload configuration',self.connect_scope),('Edit scan setup',self.edit_setup),('Read position',self.read_position),('Arm configured travel envelope',self.arm_scope),('Capture frame',self.capture)]),
                ('FOCUS AND OPTICS',[('Read objective / illumination',self.optics_status),('Apply measured objective profile',self.choose_profile),('Local autofocus',lambda:self.focus(False)),('Initial focus: absolute Z search',lambda:self.focus(True)),('Calibrate camera → stage',self.calibrate),('Save current objective profile',self.save_profile),('Optimize exposure',self.optimize_camera)]),
                ('CHIP SCANNING',[('Run full scanning workflow',self.full_workflow),('1. Survey configured region',self.survey),('2. Identify chips',self.identify),('3. Scan all identified chips',self.detail)]),
                ('REVIEW',[('Analyze saved scan',self.analyze_saved),('Load candidate results',self.load_results),('Open image review gallery',self.open_review),('Revisit selected candidate',self.revisit),('Run revisit batch',self.revisit_batch)])]
        for title,actions in groups:
            ttk.Label(controls,text=title,font=('Segoe UI',9,'bold')).pack(anchor='w',pady=(12,5))
            for label,action in actions:
                button=ttk.Button(controls,text=label,command=action);button.pack(fill='x',pady=2);self.buttons.append(button)
        ttk.Label(controls,text='Revisit selection').pack(anchor='w',pady=(6,2))
        ttk.Combobox(controls,textvariable=self.revisit_selection,values=['Kept in gallery','Large proposals needing review','All unrejected proposals'],state='readonly',width=30).pack(fill='x')
        row=ttk.Frame(controls);row.pack(fill='x',pady=4)
        ttk.Label(row,text='Batch limit').pack(side='left');ttk.Entry(row,textvariable=self.revisit_limit,width=7).pack(side='right')
        ttk.Checkbutton(controls,text='Resume matching scan',variable=self.resume).pack(anchor='w',pady=8)
        ttk.Label(controls,text='MANUAL POSITIONING',font=('Segoe UI',9,'bold')).pack(anchor='w',pady=(12,5))
        self.jog_step=tk.StringVar(value='10')
        ttk.Combobox(controls,textvariable=self.jog_step,values=['1','5','10','50','100'],width=12,state='readonly').pack(anchor='w')
        jog=ttk.Frame(controls);jog.pack(fill='x',pady=5)
        for i,(axis,sign) in enumerate([('x',-1),('x',1),('y',-1),('y',1),('z',-1),('z',1)]):
            b=ttk.Button(jog,text=axis.upper()+(' −' if sign<0 else ' +'),width=9,command=lambda a=axis,v=sign:self.jog(a,v))
            b.grid(row=i//2,column=i%2,padx=2,pady=2);self.buttons.append(b)
        ttk.Label(controls,text='Manual lens handoff:\n1. Change the objective physically.\n2. Apply its measured profile.\n3. Arm, focus, then revisit.\n\n20× is the current survey lens.\nCoordinates and distances: µm.\nProposals need image review; layer identity is unclassified.',wraplength=240).pack(anchor='w',pady=8)
        right=ttk.Panedwindow(body,orient='vertical');body.add(right,weight=1)
        self.canvas=tk.Canvas(right,bg='#13202c',highlightthickness=0,height=420);right.add(self.canvas,weight=3)
        self.canvas.create_text(350,180,text='Connect and capture to inspect the specimen',fill='#bdcbd7',font=('Segoe UI',15))
        table=ttk.Frame(right);right.add(table,weight=2)
        self.tree=ttk.Treeview(table,columns=('chip','tile','label','area','x','y'),show='headings',height=9)
        for col,label,width in [('chip','Chip',75),('tile','Tile',105),('label','Color / classification',150),('area','Area µm²',80),('x','X µm',100),('y','Y µm',100)]:
            self.tree.heading(col,text=label);self.tree.column(col,width=width)
        self.tree.pack(fill='both',expand=True);self.tree.bind('<<TreeviewSelect>>',self.show_candidate)
        self.log=tk.Text(root,height=5,state='disabled',font=('Consolas',9));self.log.pack(fill='x',padx=12)
        bottom=ttk.Frame(root,padding=12);bottom.pack(fill='x')
        ttk.Label(bottom,textvariable=self.status).pack(side='left')
        self.progress=ttk.Progressbar(bottom,length=220);self.progress.pack(side='right')
        root.after(100,self.pump)
        root.bind('<Escape>',lambda _:self.stop())

    def edit_setup(self):
        if self.config is None:return self.log_message('Connect / load a configuration first')
        dialog=tk.Toplevel(self.root);dialog.title('Scan setup · all distances in µm');dialog.transient(self.root)
        fields={};row=0
        groups=[('limits',['x_min','x_max','y_min','y_max','z_min','z_max','max_xy_step_um','max_z_step_um']),
                ('initial_focus',['low','high','coarse_step_um','fine_step_um']),
                ('focus',['half_range_um','coarse_step_um','fine_step_um'])]
        for group,names in groups:
            ttk.Label(dialog,text=group.replace('_',' ').title(),font=('Segoe UI',10,'bold')).grid(row=row,column=0,columnspan=4,sticky='w',padx=12,pady=8);row+=1
            for i,name in enumerate(names):
                col=(i%2)*2
                ttk.Label(dialog,text=name).grid(row=row+i//2,column=col,padx=10,pady=3,sticky='w')
                value=tk.StringVar(value=str((self.config.get(group) or {}).get(name,'')))
                ttk.Entry(dialog,textvariable=value,width=14).grid(row=row+i//2,column=col+1,padx=8)
                fields[(group,name)]=value
            row+=(len(names)+1)//2
        ttk.Label(dialog,text='Survey rectangle: x_min, x_max, y_min, y_max',font=('Segoe UI',10,'bold')).grid(row=row,column=0,columnspan=4,sticky='w',padx=12,pady=8);row+=1
        bounds=tk.StringVar(value=', '.join(map(str,(self.config.get('survey') or {}).get('bounds_um',[]))))
        ttk.Entry(dialog,textvariable=bounds,width=60).grid(row=row,column=0,columnspan=4,padx=12,pady=5);row+=1
        ttk.Label(dialog,text='Sample ID (change when remounting/replacing sample)').grid(row=row,column=0,columnspan=4,sticky='w',padx=12);row+=1
        sample=tk.StringVar(value=self.config.get('context',{}).get('sample_id',''))
        ttk.Entry(dialog,textvariable=sample,width=60).grid(row=row,column=0,columnspan=4,padx=12,pady=5);row+=1
        material=tk.StringVar(value=self.config.get('context',{}).get('material','unspecified'))
        ttk.Label(dialog,text='Material profile / name').grid(row=row,column=0,sticky='w',padx=12)
        ttk.Entry(dialog,textvariable=material).grid(row=row,column=1,columnspan=3,sticky='ew',padx=12);row+=1
        grid_shape=tk.StringVar(value=', '.join(map(str,(self.config.get('grid') or {}).get('grid_shape') or [])))
        count=tk.StringVar(value=str((self.config.get('grid') or {}).get('expected_count') or ''))
        ttk.Label(dialog,text='Grid rows, columns (blank = arbitrary layout)').grid(row=row,column=0,columnspan=3,sticky='w',padx=12)
        ttk.Entry(dialog,textvariable=grid_shape,width=12).grid(row=row,column=3,padx=12);row+=1
        ttk.Label(dialog,text='Expected chip count (blank = discover)').grid(row=row,column=0,columnspan=3,sticky='w',padx=12)
        ttk.Entry(dialog,textvariable=count,width=12).grid(row=row,column=3,padx=12);row+=1
        def save():
            try:
                import copy
                from .model import Limits
                from .scan import ScanPlan
                cfg=copy.deepcopy(self.config)
                for (group,name),value in fields.items():
                    if cfg.get(group) is None:cfg[group]={}
                    cfg[group][name]=float(value.get())
                Limits(**cfg['limits'])
                if cfg.get('survey') is None:cfg['survey']={'mode':'survey','autofocus_every':0}
                cfg['survey']['bounds_um']=[float(v.strip()) for v in bounds.get().split(',')]
                ScanPlan(**cfg['survey'])
                cfg.setdefault('context',{})['sample_id']=sample.get().strip()
                cfg['context']['material']=material.get().strip() or 'unspecified'
                shape=[int(v.strip()) for v in grid_shape.get().split(',')] if grid_shape.get().strip() else None
                if shape and (len(shape)!=2 or min(shape)<1):raise ValueError('Grid shape requires rows, columns.')
                expected=int(count.get()) if count.get().strip() else None
                if expected is not None and expected<1:raise ValueError('Expected count must be positive.')
                cfg.setdefault('grid',{}).update(grid_shape=shape,expected_count=expected)
                if not cfg['context']['sample_id']:raise ValueError('Set a sample ID for coordinate provenance.')
                atomic_json(Path(self.config_path.get()),cfg);self.config=cfg
                if self.scope:self.scope.armed=False
                self.status.set('Setup saved · motion disabled');dialog.destroy()
            except Exception as e:messagebox.showerror('Invalid setup',str(e),parent=dialog)
        ttk.Button(dialog,text='Save setup and disarm',command=save).grid(row=row,column=0,columnspan=4,pady=12)

    def jog(self,axis,sign):
        amount=float(self.jog_step.get())*sign
        def action():
            from .workflow import move_z_bounded
            s=self.require_scope();p=s.position()
            if axis=='z':move_z_bounded(s,p.z+amount)
            else:s.travel(p.x+(amount if axis=='x' else 0),p.y+(amount if axis=='y' else 0))
            self.event('frame',s.capture(),{});return f'Moved {axis.upper()} by {amount:g} µm'
        self.worker('Positioning…',action)

    def log_message(self,text):
        self.log.configure(state='normal');self.log.insert('end',time.strftime('%H:%M:%S ')+str(text)+'\n');self.log.see('end');self.log.configure(state='disabled')

    def choose_config(self):
        file=filedialog.askopenfilename(filetypes=[('Configuration','*.json')])
        if file:self.config_path.set(file)

    def choose_output(self):
        folder=filedialog.askdirectory()
        if folder:
            from .storage import resolve_output_path
            try:self.output.set(str(resolve_output_path(folder)))
            except RuntimeError as exc:self.log_message(str(exc))

    def worker(self,title,fn):
        if self.busy or self.stopping:return
        self.busy=True;self.status.set(title)
        for b in self.buttons:b.configure(state='disabled')
        def run():
            try:self.events.put(('done',title,fn()))
            except BaseException as e:
                self.events.put(('error',str(e),traceback.format_exc()))
            finally:self.events.put(('idle',None,None))
        threading.Thread(target=run,daemon=True).start()

    def require_scope(self):
        if self.scope is None:raise SafetyError('Connect a microscope first.')
        return self.scope

    def output_folder(self):
        from .storage import resolve_output_path
        return resolve_output_path(self.output.get())

    def event(self,event,frame,info):self.events.put(('progress',event,(frame,info)))

    def connect_scope(self):
        path=self.config_path.get()
        def action():
            cfg=load_config(path)
            from .storage import resolve_output_path
            resolve_output_path(self.output.get())
            if self.scope is not None:self.scope.close()
            self.scope=None
            self.scope=connect(cfg);self.config=cfg;self.active_profile=None;self.loaded_config_path=Path(path).resolve()
            self.events.put(('optics',describe_optics(cfg),None))
            if cfg['backend']=='zen':
                controls=self.scope.hardware_controls()
                self.events.put(('optics',describe_optics(cfg,controls.objectives(),controls.lamp_percent()),None))
            p=self.scope.position();return f'{cfg["backend"].upper()} connected; motion disabled. XYZ {asdict(p)}'
        self.worker('Connecting…',action)

    def read_position(self):self.worker('Reading position…',lambda:asdict(self.require_scope().position()))
    def arm_scope(self):
        self.worker('Checking travel envelope and objective…',self.arm_active_configuration)

    def arm_active_configuration(self):
        s=self.require_scope();arm(s,self.config)
        try:
            if self.config['backend']=='zen':
                controls=s.hardware_controls();state=controls.objectives()
                expected=self.config.get('zen',{}).get('expected_objective_position')
                if not state.get('valid_position') or (expected is not None and state['position']!=expected):
                    raise SafetyError('The manually engaged objective does not match this configuration. Apply the correct measured profile.')
                target=self.config.get('context',{}).get('illumination_percent')
                if target is not None:controls.set_lamp_percent(target)
                state=controls.verify_optics(expected)
                self.events.put(('optics',describe_optics(self.config,state,controls.lamp_percent()),None))
        except BaseException:
            s.armed=False;raise
        return 'Motion armed within configured bounds; objective and optical profile checked'

    def capture(self):
        folder=self.output_folder()/'captures';name=time.strftime('%Y%m%d-%H%M%S')
        def action():
            f=self.require_scope().capture();save_frame(folder,name,f);self.event('frame',f,{});return 'Raw frame saved'
        self.worker('Acquiring frame…',action)

    def focus(self,wide):
        from .autofocus import autofocus,autofocus_range
        folder=self.output_folder()/'focus'/time.strftime('%Y%m%d-%H%M%S')
        def action():
            s=self.require_scope();cb=lambda f,m:self.event('focus',f,m)
            if wide and not self.config.get('initial_focus'):raise SafetyError('Set an absolute Z search interval for this objective in Edit scan setup, or use local autofocus around the current height.')
            result=autofocus_range(s,**self.config['initial_focus'],output=folder,callback=cb) if wide else autofocus(s,**self.config['focus'],output=folder,callback=cb)
            return f'{result.reason}; Z={result.z_um:.2f} µm; prominence={result.prominence:.3f}'
        self.worker('Searching initial focus…' if wide else 'Autofocusing…',action)

    def calibrate(self):
        from .calibration import calibrate_camera
        path=self.config_path.get()
        def action():
            self.config['camera_transform']=None
            atomic_json(Path(path),self.config)
            t,q=calibrate_camera(self.require_scope(),self.config.get('calibration_step_um',20));save_transform(path,self.config,t,q);return 'Camera transform calibrated and saved'
        self.worker('Measuring camera / stage transform…',action)

    def survey(self):
        from .scan import ScanPlan,run_scan
        folder=self.output_folder()/'survey';resume=self.resume.get()
        self.worker('Acquiring chip overview…',lambda:run_scan(self.require_scope(),ScanPlan(**self.config['survey']),transform_from_config(self.config),folder,context=self.config.get('context'),callback=self.event,resume=resume)['state'])

    def identify(self):
        from .scan import identify_chips
        folder=self.output_folder()/'survey'
        def action():
            chips=identify_chips(folder,**self.config.get('grid',{}));self.events.put(('image',str(folder/'chips.png'),None));return f'{len(chips)} chips identified; map saved'
        self.worker('Identifying chips…',action)

    def detail(self):
        from .workflow import scan_chips
        from .segmentation import ContrastModel
        from .report import write_report
        folder=self.output_folder();resume=self.resume.get()
        def action():
            model=ContrastModel.load(self.config['material_model']) if self.config.get('material_model') else None
            result=scan_chips(self.require_scope(),transform_from_config(self.config),folder/'survey',folder/'detail',self.config,model,self.event,resume)
            write_report(folder/'detail');self.events.put(('results',str(folder/'detail/analysis'),None));return result['state']
        self.worker('Scanning chips…',action)

    def full_workflow(self):
        folder=self.output_folder();resume=self.resume.get()
        def action():
            from .autofocus import autofocus_range
            from .scan import ScanPlan,run_scan,identify_chips
            from .workflow import scan_chips
            from .segmentation import ContrastModel
            s=self.require_scope();self.arm_active_configuration()
            if not resume:
                if not self.config.get('initial_focus'):raise SafetyError('Set this objective\'s initial focus interval in Edit scan setup before running the full workflow.')
                result=autofocus_range(s,**self.config['initial_focus'],output=folder/'initial-focus',callback=lambda f,m:self.event('focus',f,m))
                if not result.success:raise SafetyError(result.reason)
            transform=transform_from_config(self.config)
            run_scan(s,ScanPlan(**self.config['survey']),transform,folder/'survey',context=self.config.get('context'),callback=self.event,resume=resume)
            identify_chips(folder/'survey',**self.config.get('grid',{}))
            model=ContrastModel.load(self.config['material_model']) if self.config.get('material_model') else None
            result=scan_chips(s,transform,folder/'survey',folder/'detail',self.config,model,self.event,resume)
            self.events.put(('results',str(folder/'detail/analysis'),None));return result['state']
        self.worker('Survey → chip identification → focused scan → color analysis…',action)

    def analyze_saved(self):
        folder=filedialog.askdirectory(title='Select a scan or a multi-chip detail folder')
        if not folder:return
        def action():
            from .analysis import analyze_scan
            from .storage import resolve_output_path
            destination=resolve_output_path(Path(folder)/'analysis')
            result=analyze_scan(folder,(self.config or {}).get('analysis'),output=destination,callback=lambda phase,info:self.event('analysis',None,{'phase':phase,**info}))
            from .storage import managed_output_path
            self.events.put(('results',str(managed_output_path(Path(folder)/'analysis')),None))
            return f'{result["candidate_count"]} unverified proposals; {result["rejected_count"]} filtered regions available for review'
        self.worker('Analyzing raw tiles; proposals require review…',action)

    def open_review(self):
        if self.results_folder is None:return self.log_message('Load candidate results first.')
        from .review import serve_review
        server,url=serve_review(self.results_folder,open_browser=True)
        self.review_servers.append(server);self.log_message('Flake gallery: '+url)

    def optics_status(self):
        def action():
            controls=self.require_scope().hardware_controls()
            state=controls.objectives();lamp=controls.lamp_percent()
            self.events.put(('optics',describe_optics(self.config,state,lamp),None))
            return json.dumps({'objectives':state,'illumination_percent':lamp},indent=2)
        self.worker('Reading microscope optics…',action)

    def choose_profile(self):
        if self.busy or self.stopping:return
        file=filedialog.askopenfilename(title='Choose the measured profile for the manually engaged objective',initialdir=str(Path(self.config_path.get()).parent),initialfile=Path(self.profile_path.get()).name if self.profile_path.get() else '',filetypes=[('Objective profile','objective*.json'),('JSON','*.json')])
        if not file:return
        self.profile_path.set(file)
        session=self.output_folder()
        def action():
            s=self.require_scope();profile=json.loads(Path(file).read_text(encoding='utf-8-sig'))
            controls=s.hardware_controls();state=controls.objectives()
            cfg=configuration_for_objective_profile(self.config,profile,state)
            # Applying a profile only changes the acquisition configuration. It performs no motion.
            s.armed=False;s.job_template=cfg['zen']['job_template'];s.camera_resources=dict(cfg['zen']['camera_resources'])
            s.expected_objective_position=cfg['zen']['expected_objective_position'];s.expected_scale=tuple(cfg['camera_transform']['pixel_size_um'])
            self.config=cfg;self.active_profile=profile
            path=session/f'active-objective-{profile["objective"]["position"]}.json'
            atomic_json(path,cfg);self.loaded_config_path=path.resolve()
            self.events.put(('config_path',str(path.resolve()),None))
            self.events.put(('optics',describe_optics(cfg,state,controls.lamp_percent()),None))
            return f'Applied {profile["objective"]["name"]} profile for this session. Arm, focus, then scan or revisit.'
        self.worker('Checking objective and applying measured profile…',action)

    def save_profile(self):
        file=filedialog.asksaveasfilename(title='Save profile for the currently calibrated objective',defaultextension='.json',filetypes=[('Objective profile','*.json')])
        if not file:return
        def action():
            from .optics import save_objective_profile
            result=save_objective_profile(self.require_scope(),self.config,transform_from_config(self.config),file)
            self.active_profile=result;self.events.put(('profile_path',file,None))
            return 'Saved '+result['objective']['name']+' profile: '+file
        self.worker('Verifying objective and camera scale…',action)

    def optimize_camera(self):
        folder=self.output_folder()/('exposure-'+time.strftime('%H%M%S'))
        def action():
            from .optics import optimize_exposure
            initial=self.config.get('zen',{}).get('camera_resources',{}).get('ff_exposure_ms',200)
            result=optimize_exposure(self.require_scope(),folder,initial_ms=initial)
            self.config.setdefault('zen',{}).setdefault('camera_resources',{})['ff_exposure_ms']=result['exposure_ms']
            self.config.setdefault('context',{})['exposure_ms']=result['exposure_ms']
            self.events.put(('optics',describe_optics(self.config),None))
            return f'Fixed exposure {result["exposure_ms"]:.2f} ms; save a new objective profile to use it for revisits.'
        self.worker('Measuring camera exposure…',action)

    def revisit_batch(self):
        if self.results_folder is None:return self.log_message('Load analyzed candidate results first.')
        file=self.profile_path.get()
        if not file:return self.log_message('Select / apply a measured objective profile first.')
        selections={'Kept in gallery':'kept','Large proposals needing review':'priority','All unrejected proposals':'all'}
        selection=selections[self.revisit_selection.get()]
        try:
            limit=int(self.revisit_limit.get())
            if limit<1:raise ValueError()
        except ValueError:return self.log_message('Batch limit must be a positive integer.')
        folder=self.output_folder()/('revisits-'+time.strftime('%H%M%S'));analysis=self.results_folder
        def action():
            from .revisit import run_revisits
            profile=json.loads(Path(file).read_text(encoding='utf-8-sig'))
            prepared=configuration_for_objective_profile(self.config,profile,self.require_scope().hardware_controls().objectives())
            result=run_revisits(self.require_scope(),analysis,profile,folder,selection=selection,max_candidates=limit,callback=self.event,analysis_options=self.config.get('analysis'))
            self.config=prepared;self.active_profile=profile
            self.events.put(('optics',describe_optics(self.config,self.require_scope().hardware_controls().objectives(),self.require_scope().hardware_controls().lamp_percent()),None))
            if result.get('analysis_folder'):self.events.put(('results',result['analysis_folder'],None))
            return f'{result["verified_count"]} fields registered to source images; flake identity remains unverified. Images saved in {folder}'
        self.worker('Revisiting, focusing and registering source fields…',action)

    def load_results(self):
        file=filedialog.askopenfilename(initialdir=self.output.get(),filetypes=[('Flake results','flakes.json')])
        if file:self.populate_results(Path(file).parent)

    def populate_results(self,folder):
        try:
            self.results_folder=Path(folder);payload=json.loads((self.results_folder/'flakes.json').read_text())
            self.results_source=Path(payload.get('source_folder',self.results_folder)).resolve()
            self.flakes=[f for f in payload['flakes'] if f.get('disposition')!='rejected']
            self.tree.delete(*self.tree.get_children())
            for i,f in enumerate(self.flakes):
                xy=f.get('stage_xy_um') or [0,0]
                self.tree.insert('', 'end',iid=str(i),values=(f.get('chip_id'),f['tile_id'],f.get('color_group',f['label']),f'{f["area_um2"]:.1f}',f'{xy[0]:.2f}',f'{xy[1]:.2f}'))
            note=' · FAILED prior user review; inspect raw images before selecting revisits' if payload.get('validation_status')=='failed_user_review' else ' · unverified proposals'
            self.log_message(f'Loaded {len(self.flakes)} candidate regions'+note)
        except Exception as e:messagebox.showerror('Cannot load results',str(e))

    def candidate_file(self,f):
        if f.get('source_image'):
            path=(self.results_source/f['source_image']).resolve()
            if not path.is_relative_to(self.results_source):raise ValueError('Invalid candidate image path')
            return path
        nested=self.results_folder/(f.get('chip_id') or '')/'tiles'/(f['tile_id']+'.tiff')
        return nested if nested.exists() else self.results_folder/'tiles'/(f['tile_id']+'.tiff')

    def show_candidate(self,_=None):
        selected=self.tree.selection()
        if not selected:return
        try:
            f=self.flakes[int(selected[0])];frame=load_frame(self.candidate_file(f));rgb=preview(frame.rgb)
            from .review import crop_bounds
            x0,y0,x1,y1=crop_bounds(f,rgb.shape,frame.pixel_size_um)
            cv2.polylines(rgb,[np.int32(f['polygon_px'])],True,(255,220,0),2);self.show_image(rgb[y0:y1,x0:x1])
            self.status.set(f'{f["id"]} · {f["tile_id"]} · area {f["area_um2"]:.1f} µm²')
        except Exception as e:self.log_message(e)

    def revisit(self):
        selected=self.tree.selection()
        if not selected:return self.log_message('Select a candidate first')
        f=self.flakes[int(selected[0])];source=self.candidate_file(f)
        folder=self.output_folder()/'selected-revisits'/time.strftime('%Y%m%d-%H%M%S')
        def action():
            from .autofocus import autofocus
            from .revisit import register_candidate
            s=self.require_scope();saved=load_frame(source);current=s.capture()
            if not s.armed:raise SafetyError('Arm the configured travel envelope before revisiting.')
            transform=transform_from_config(self.config);transform.validate_frame(current)
            if not transform.calibrated:raise SafetyError('Apply or measure a camera transform for the engaged objective.')
            if saved.metadata.get('source')!=current.metadata.get('source'):raise SafetyError('Candidate and connected microscope sources differ.')
            manifest_path=source.parent.parent/'manifest.json'
            manifest=json.loads(manifest_path.read_text())
            sample=manifest.get('context',{}).get('sample_id')
            if not sample or sample!=self.config.get('context',{}).get('sample_id'):raise SafetyError('Saved candidate belongs to another sample.')
            # Revisit XY only; never replay a historic sample height without refocusing.
            s.travel(*f['stage_xy_um']);result=autofocus(s,**self.config['focus'],output=folder/'focus',callback=lambda f,m:self.event('focus',f,m))
            if not result.success:raise SafetyError(result.reason)
            frame=s.capture();transform.validate_frame(frame);save_frame(folder,'revisit',frame)
            record={'candidate_id':f['id'],'source_image':str(source),'position':asdict(frame.position),
                    'camera_transform':asdict(transform),'context':self.config.get('context'),
                    'field_registration_verified':False,'flake_identity_verified':False}
            try:
                record['registration']=register_candidate(saved,frame,CameraTransform(**manifest['transform']),transform,f['centroid_px'])
                record['field_registration_verified']=True
            except AcquisitionError as exc:record['registration_error']=str(exc)
            finally:atomic_json(folder/'registration.json',record);self.event('frame',frame,{})
            if not record['field_registration_verified']:return f'Image saved but field registration did not pass: {record["registration_error"]}. {folder}'
            return f'Selected field reached, refocused and registered; inspect the saved image for the actual flake. {folder}'
        self.worker('Revisiting candidate…',action)

    def stop(self):
        if self.stopping:return
        self.status.set('Stopping / disarming…')
        if self.scope:
            self.stopping=True
            self.scope.cancel.set();self.scope.armed=False
            def action():
                errors=self.scope.stop();self.events.put(('stop',errors,None))
            threading.Thread(target=action,daemon=True).start()
        else:self.status.set('No microscope connected · motion disabled')

    def show_image(self,rgb):
        im=Image.fromarray(rgb);im.thumbnail((max(self.canvas.winfo_width(),500),max(self.canvas.winfo_height(),300)))
        self.photo=ImageTk.PhotoImage(im);self.canvas.delete('all');self.canvas.create_image(self.canvas.winfo_width()/2,self.canvas.winfo_height()/2,image=self.photo)

    def pump(self):
        try:
            while True:
                kind,a,b=self.events.get_nowait()
                if kind=='idle':
                    self.busy=False
                    for button in self.buttons:button.configure(state='normal')
                elif kind=='done':self.status.set(str(b));self.log_message(str(b))
                elif kind=='error':self.status.set('Stopped: '+a);self.log_message(a)
                elif kind=='stop':
                    self.stopping=False
                    if a:self.closing=False
                    self.status.set('STOP returned errors: '+'; '.join(a) if a else 'Stopped · motion disabled');self.log_message(self.status.get())
                elif kind=='progress':
                    f,info=b
                    if f is not None:
                        self.last_frame=f;self.show_image(preview(f.rgb));p=f.position;self.position.set(f'X {p.x:.2f}   Y {p.y:.2f}   Z {p.z:.2f} µm')
                    if a=='tile':self.progress['value']=100*info['index']/info['total'];self.status.set(f'Tile {info["index"]} / {info["total"]} · {info["flakes"]} candidates')
                    elif a=='focus':self.status.set(f'Focus search · Z {info["z_um"]:.2f} µm · score {info["score"]:.5g}')
                    elif a=='chip':self.log_message('Scanning chip '+info['chip_id'])
                    elif a=='revisit':self.status.set(f'Revisit {info["index"]} / {info["total"]} · field registered: {info["verified"]}');self.progress['value']=100*info['index']/info['total']
                    elif a=='analysis':self.status.set(f'{info["phase"]} · {info["index"]} / {info["total"]}');self.progress['value']=100*info['index']/info['total']
                elif kind=='image':self.show_image(np.array(Image.open(a).convert('RGB')))
                elif kind=='optics':self.optical_status.set(a)
                elif kind=='profile_path':self.profile_path.set(a)
                elif kind=='config_path':self.config_path.set(a)
                elif kind=='results':self.populate_results(a)
        except queue.Empty:pass
        if self.closing and not self.busy and not self.stopping:
            for server in self.review_servers:server.shutdown();server.server_close()
            if self.scope:self.scope.close()
            self.root.destroy();return
        self.root.after(100,self.pump)

    def close(self):
        if self.busy or self.stopping:
            self.closing=True;self.stop();self.status.set('Stopping acquisition before closing…')
        else:
            for server in self.review_servers:server.shutdown();server.server_close()
            if self.scope:self.scope.close()
            self.root.destroy()

def launch(config_path):
    root=tk.Tk();ttk.Style().theme_use('clam');App(root,config_path);root.mainloop()
