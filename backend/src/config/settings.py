import os
DATABASE_URL=os.getenv('DATABASE_URL','mysql+pymysql://ordinal:ordinal-demo-local@localhost:17731/ordinal?charset=utf8mb4')
REDIS_URL=os.getenv('REDIS_URL','redis://localhost:6379/0')
JWT_SECRET=os.getenv('JWT_SECRET','local-ordinal-fibers-change-this-before-hosting')
SEED_DEMO=os.getenv('SEED_DEMO','false').lower()=='true'
# 可复算置换检验：分配数不超过该阈值时穷举，否则按固定种子抽样并报告 Monte Carlo 误差。
PERM_SEED=int(os.getenv('PERM_SEED','20261112'))
PERM_EXHAUSTIVE_LIMIT=int(os.getenv('PERM_EXHAUSTIVE_LIMIT','100000'))
PERM_RANDOM_DRAWS=int(os.getenv('PERM_RANDOM_DRAWS','20000'))
PERM_BATCH_SIZE=int(os.getenv('PERM_BATCH_SIZE','500'))
