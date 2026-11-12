import json,os,tempfile
from pathlib import Path
from sqlalchemy import select,func
from fastapi import HTTPException
from redis import Redis
from src.config.settings import (REDIS_URL,PERM_SEED,PERM_EXHAUSTIVE_LIMIT,
                                 PERM_RANDOM_DRAWS,PERM_BATCH_SIZE)
from src.models import RankTestRun
from src.services.study_service import get_study
from src.services.treatment_service import list_treatments
from src.services.reading_service import list_readings
from src.services.rank_inference import analyze_study
from src.services.permutation import plan_permutation,initial_progress,run_permutation
from src.constants.enums import RunStatus
redis=Redis.from_url(REDIS_URL)
ARTIFACT_DIR=Path('/artifacts')

def list_runs(db):return db.scalars(select(RankTestRun).order_by(RankTestRun.id.desc())).all()
def get_run(db,id):
    r=db.get(RankTestRun,id)
    if not r:raise HTTPException(404,'秩检验任务不存在')
    return r

def create_run(db,data):
    s=get_study(db,data.study_id)
    if db.scalar(select(RankTestRun).where(RankTestRun.name==data.name)):raise HTTPException(409,'任务名称重复')
    if db.scalar(select(func.count()).select_from(RankTestRun).where(RankTestRun.status.in_(['queued','running'])))>=3:raise HTTPException(429,'已有3项活动任务，请等待')
    snapshot={'study':{k:getattr(s,k) for k in ('id','code','name','protocol','source_file')},'treatments':[{k:getattr(t,k) for k in ('id','code','name','process_note')} for t in list_treatments(db,s.id)],'readings':[{k:getattr(r,k) for k in ('id','specimen_code','treatment_id','grade','source_note')} for r in list_readings(db,s.id)]}
    r=RankTestRun(**data.model_dump(),status=RunStatus.QUEUED,snapshot=snapshot);db.add(r);db.commit()
    try:redis.lpush('ordinal:runs',str(r.id))
    except Exception:pass
    return r

def _write_artifact(run_id,result):
    """同目录临时文件 + os.replace 原子替换，保证工件与数据库检查点内容一致。"""
    ARTIFACT_DIR.mkdir(exist_ok=True)
    payload=json.dumps(result,ensure_ascii=False,allow_nan=False)
    fd,tmp=tempfile.mkstemp(prefix=f'run-{run_id}-',suffix='.tmp',dir=str(ARTIFACT_DIR))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(payload)
        os.replace(tmp,str(ARTIFACT_DIR/f'run-{run_id}.json'))
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def _perm_settings():
    return {'seed':PERM_SEED,'exhaustive_limit':PERM_EXHAUSTIVE_LIMIT,
            'random_draws':PERM_RANDOM_DRAWS,'batch_size':PERM_BATCH_SIZE}

def retry_run(db,id):
    """把因 worker 中断而失败的任务重新排队；存在置换检查点时从断点续算，不重复计数。"""
    r=get_run(db,id)
    if r.status!=RunStatus.FAILED:raise HTTPException(409,'仅失败任务可重新排队')
    checkpoint=(r.result or {}).get('permutation')
    if not checkpoint or checkpoint.get('status')!='running':
        raise HTTPException(409,'失败发生在置换计算开始前（多为输入校验）；请核对数据后另建任务，旧结果保持原样')
    r.status=RunStatus.QUEUED;r.error='';db.commit()
    try:redis.lpush('ordinal:runs',str(r.id))
    except Exception:pass
    return r

def execute_run(db,id):
    r=get_run(db,id)
    if r.status not in (RunStatus.QUEUED,RunStatus.RUNNING):return
    r.status=RunStatus.RUNNING;db.commit()
    try:
        # 冻结快照重算渐近结果：确定性输出，中断重算与首次完全一致；旧任务结果永不回写。
        result=analyze_study(r.snapshot,r.alpha)
        plan=plan_permutation(result['group_sizes'],PERM_EXHAUSTIVE_LIMIT)
        previous=r.result or {}
        prior=previous.get('permutation')
        # 恢复条件：同模式/目标/种子的未完成检查点才续算；否则从头开始一条确定序列。
        resuming=bool(
            prior and prior.get('status')=='running'
            and prior.get('mode')==plan['mode']
            and prior.get('seed')==PERM_SEED
            and prior.get('group_sizes')==plan['group_sizes']
            and (prior.get('target_allocations')
                 ==(plan['n_allocations'] if plan['mode']=='exhaustive' else PERM_RANDOM_DRAWS))
        )
        if resuming:
            progress=prior
            progress['resume_count']=int(progress.get('resume_count',0))+1
        else:
            progress=initial_progress(plan,PERM_SEED,PERM_RANDOM_DRAWS,PERM_BATCH_SIZE)
        result['permutation']=progress
        result['permutation_reject']=False  # 完成后按同一预设 alpha 判定

        def checkpoint(pr):
            # 每批一次原子提交：数据库 JSON 与工件同时落盘，崩溃只丢当前未提交批。
            result['permutation']=pr
            result['permutation_reject']=(pr.get('status')=='completed'
                                           and pr.get('p_value') is not None
                                           and pr['p_value']<=r.alpha)
            r.result=result;db.commit()
            _write_artifact(id,result)

        run_permutation(result['pooled_ranks'],result['group_sizes'],result['h'],
                        progress,_perm_settings(),checkpoint)
        # 收尾再提交一次，确保最终 p 值/判定与工件一致。
        result['permutation']=progress
        result['permutation_reject']=progress['p_value']<=r.alpha
        r.result=result;r.error='';r.status=RunStatus.COMPLETED;db.commit()
        _write_artifact(id,result)
    except Exception as exc:
        db.rollback();r=get_run(db,id);r.status=RunStatus.FAILED;r.error=str(exc)[:600];db.commit()
