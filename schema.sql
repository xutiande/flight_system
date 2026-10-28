set names utf8mb4;

create database if not exists csair
    default character set utf8mb4
    collate utf8mb4_general_ci;

use csair;

-- ============================================================
-- 5.1.1 城市表
-- ============================================================
create table if not exists city (
    city_code     varchar(8)  not null                  comment 'IATA 城市码，如 BJS/SHA/CTU',
    name_cn       varchar(32) not null                  comment '城市中文名',
    country       varchar(32) not null default '中国'   comment '国家/地区（中文）',
    province      varchar(32) default null              comment '所属省份/州',
    airport_count tinyint unsigned not null default 0   comment '关联机场数量（冗余缓存，机场增删时由应用层同步）',
    primary key (city_code),
    key idx_country (country)
) engine = innodb default charset = utf8mb4 collate = utf8mb4_general_ci comment = '城市';

-- ============================================================
-- 5.1.2 机场表
-- ============================================================
create table if not exists airport (
    iata      char(3)     not null                  comment 'IATA 三字码，如 CAN/PKX/TFU',
    name_cn   varchar(64) not null                  comment '机场中文名',
    city_code varchar(8)  not null                  comment '所属城市，关联 city.city_code',
    country   varchar(32) not null default '中国'   comment '国家/地区（中文）；与 city.country 冗余，写入需保持一致',
    primary key (iata),
    key idx_city (city_code),
    constraint fk_airport_city foreign key (city_code) references city (city_code)
) engine = innodb default charset = utf8mb4 collate = utf8mb4_general_ci comment = '机场';

-- ============================================================
-- 5.1.3 航线表（机场对，有向）
-- ============================================================
create table if not exists route (
    id               int unsigned      not null auto_increment,
    dep_iata         char(3)           not null       comment '出发机场三字码',
    arr_iata         char(3)           not null       comment '到达机场三字码',
    status           enum('candidate', 'active', 'dormant', 'retired') not null default 'candidate' comment 'candidate 候选 / active 已确认 / dormant 停飞 / retired 退役',
    source           enum('official', 'probe', 'emergent', 'manual') default null   comment '航线来源',
    hit_count        int unsigned      not null default 0   comment '连续命中次数（置信度）',
    miss_count       int unsigned      not null default 0   comment '连续未命中次数（降级依据）',
    flight_freq      tinyint unsigned  not null default 0   comment '日均班次，由采集结果反推',
    fetch_interval   int unsigned      not null default 1440 comment '采集间隔（分钟）',
    priority         tinyint unsigned  not null default 3   comment '1 高 / 2 中 / 3 低',
    last_verified_at datetime          default null   comment '最近一次确认有航班的时间',
    next_run_at      datetime          default null   comment '下次应采集时间（调度依据）',
    created_at       datetime          not null default current_timestamp,
    updated_at       datetime          not null default current_timestamp on update current_timestamp,
    primary key (id),
    unique key uk_pair (dep_iata, arr_iata),
    key idx_pick (status, next_run_at, priority),
    constraint fk_route_dep foreign key (dep_iata) references airport (iata),
    constraint fk_route_arr foreign key (arr_iata) references airport (iata)
) engine = innodb default charset = utf8mb4 collate = utf8mb4_general_ci comment = '航线（机场对，有向）';

-- ============================================================
-- 5.1.4 航班表（只存静态属性，价格与余票在 price_snapshot）
-- ============================================================
create table if not exists flight (
    flight_no           varchar(16)       not null comment '销售航班号，如 CZ3123',
    dep_date            date              not null comment '出发日期',
    dep_iata            char(3)           not null comment '出发机场三字码',
    arr_iata            char(3)           not null comment '到达机场三字码',
    arr_date            date              default null comment '到达日期；跨天航班 != dep_date，cross_day 由 arr_date <> dep_date 推导',
    dep_time            time              default null comment '出发时间',
    arr_time            time              default null comment '到达时间',
    duration_min        smallint unsigned default null comment '飞行时长（分钟）',
    aircraft            varchar(16)       default null comment '机型 IATA 代码，如 32K/919',
    aircraft_name       varchar(32)       default null comment '机型中文名；代码共享航班常为空',
    dep_term            varchar(8)        default null comment '出发航站楼；部分机场不返回',
    arr_term            varchar(8)        default null comment '到达航站楼；部分机场不返回',
    stop_count          tinyint unsigned  not null default 0 comment '经停次数，0=直飞',
    is_code_share       tinyint           not null default 0 comment '1=代码共享，实际承运人非南航',
    marketing_flight_no varchar(16)       default null comment '实际承运方航班号；查航班动态必须用它',
    meal                char(1)           default null comment '是否提供餐食 Y/N',
    lowest_price        decimal(10, 2)    default null comment '最低票面价（不含税，源字段 lowPrice）',
    updated_at          datetime          not null default current_timestamp on update current_timestamp,
    primary key (flight_no, dep_date),
    key idx_route_date (dep_iata, arr_iata, dep_date),
    key idx_code_share (is_code_share, marketing_flight_no)
) engine = innodb default charset = utf8mb4 collate = utf8mb4_general_ci comment = '航班（静态属性）';

-- ============================================================
-- 5.1.5 航班动态表
-- 代码共享航班必须用 flight.marketing_flight_no 查询
-- ============================================================
create table if not exists flight_dynamic (
    id              bigint unsigned not null auto_increment,
    flight_no       varchar(16)     not null comment '实际执行的航班号',
    flight_date     date            not null comment '航班日期',
    status          varchar(20)     default null comment '计划/登机/起飞/到达/延误/取消/备降',
    dep_terminal    varchar(20)     default null comment '出发航站楼',
    arr_terminal    varchar(20)     default null comment '到达航站楼',
    gate            varchar(20)     default null comment '登机口',
    plan_dep_time   time            default null comment '计划出发',
    actual_dep_time time            default null comment '实际出发',
    plan_arr_time   time            default null comment '计划到达',
    actual_arr_time time            default null comment '实际到达',
    delay_reason    varchar(200)    default null comment '延误原因',
    updated_at      datetime        not null default current_timestamp on update current_timestamp,
    primary key (id),
    unique key uk_flight_date (flight_no, flight_date),
    key idx_date (flight_date)
) engine = innodb default charset = utf8mb4 collate = utf8mb4_general_ci comment = '航班动态';

-- ============================================================
-- 5.1.6 价格监控表
-- 通知限频（24h <= 3 次）由 redis 实现，不落表：
--   notify:rule:{rule_id}  incr + expire 86400
-- ============================================================

create table if not exists price_monitor (
    id             bigint unsigned not null auto_increment,
    user_id        bigint unsigned not null comment '用户 id',
    dep_city       varchar(8)      not null comment '出发城市码（用户视角）',
    arr_city       varchar(8)      not null comment '到达城市码（用户视角）',
    target_date    date            not null comment '目标日期',
    cabin_scope    varchar(16)     not null default 'ALL' comment '舱位范围：Y 仅经济舱 / J 仅公务舱 / ALL',
    max_price      decimal(10, 2)  not null comment '期望最高价（含税，对比 price_total）',
    notify_channel varchar(20)     not null default 'email' comment '通知方式',
    is_active      tinyint         not null default 1 comment '是否启用 1/0',
    created_at     datetime        not null default current_timestamp,
	last_checked_at    datetime    default null comment '上次检查时间',
    next_check_at      datetime    default null comment '下次该检查的时间',
    check_interval_min smallint    default 120 comment '当前检查间隔（分钟）',
    last_notified_at   datetime    default null comment '上次通知时间',
    last_notified_price decimal(10,2) default null comment '上次通知时的价格',
    hit_count          int         default 0 comment '累计低于阈值的次数',
    primary key (id),
    key idx_user (user_id, is_active),
    key idx_target (target_date, is_active)
) engine = innodb default charset = utf8mb4 collate = utf8mb4_general_ci comment = '价格监控规则';

-- ============================================================
-- 5.1.6.1 通知流水表
-- 排查为什么没收到通知
-- ============================================================

create table if not exists price_alert (
    id            bigint unsigned not null auto_increment,
    monitor_id    bigint unsigned not null,
    dep_city      varchar(8)  not null,
    arr_city      varchar(8)  not null,
    target_date   date        not null,
    flight_no     varchar(10) not null comment '触发的最低价的航班',
    cabin_code    varchar(4)  not null,
    price_total   decimal(10,2) not null comment '触发时的最低含税价',
    max_price     decimal(10,2) not null comment '用户设的阈值',
    channel       varchar(20) not null comment 'email / webhook',
    status        varchar(12) not null default 'pending' comment 'pending/sent/failed',
    error_msg     varchar(500) default null,
    created_at    datetime not null default current_timestamp,
    sent_at       datetime default null,
    primary key (id),
    key idx_monitor (monitor_id, created_at),
    key idx_status (status, created_at)
);

-- ============================================================
-- 5.1.7 舱位价格快照表（核心资产表，只追加不修改）
--
-- 唯一键必须包含 fare_basis：
--   实测 CZ3123 同一日期有两个 L 舱 —— LCP1WNNP 卖 960、
--   LUKWNND「新客特惠」卖 870。若唯一键缺少 fare_basis，
--   两条会互相覆盖，870 的最低价被 960 覆盖，
--   而 flight.lowest_price 记的是 870，数据自相矛盾。
--
-- 分区说明：
--   mysql 要求分区表的所有主键/唯一键都包含分区列，
--   故主键为 (id, dep_date)。
--   mysql 5.7 的分区表不支持外键，本表也未建外键。
-- ============================================================
create table if not exists price_snapshot (
    id                   bigint unsigned not null auto_increment,
    flight_no            varchar(16)     not null comment '关联 flight.flight_no',
    dep_date             date            not null comment '关联 flight.dep_date；分区键',
    cabin_code           varchar(4)      not null comment '舱位代码，如 L/V/Z/J',
    fare_basis           varchar(32)     not null default '' comment '运价基础代码，区分同舱位下不同产品',
    cabin_class          varchar(32)     default null comment '产品品牌名（如 新客特惠），仅作展示',
    base_cabin           char(1)         not null comment '舱位大类：Y 经济舱系列 / J 公务舱系列',
    price                decimal(10, 2)  not null comment '票面价（不含税）',
    tax                  decimal(10, 2)  not null default 0 comment '税费 = 机场建设费 + 燃油附加费',
    price_total          decimal(10, 2)  not null comment '含税总价，展示与比价一律用它',
    seats_left           smallint        default null comment '余票数',
    seats_is_lower_bound tinyint         not null default 0 comment '1=余票为下限值（源数据 >9，实际>=9）',
    refund_change_type   varchar(16)     default null comment '退改费用档位：退改费用高/退改费用低',
    captured_at          datetime        not null comment '采集时间，截断到小时以保证幂等',
    primary key (id, dep_date),
    unique key uk_snapshot (flight_no, dep_date, cabin_code, fare_basis, captured_at),
    key idx_trend (dep_date, captured_at),
    key idx_flight (flight_no, dep_date)
) engine = innodb default charset = utf8mb4 collate = utf8mb4_general_ci comment = '舱位价格快照'
partition by range (to_days(dep_date)) (
    partition p_2026_10 values less than (to_days('2026-11-01')),
    partition p_2026_11 values less than (to_days('2026-12-01')),
    partition p_2026_12 values less than (to_days('2027-01-01')),
    partition pmax      values less than maxvalue
);


create table if not exists crawl_task (
    id          bigint unsigned  not null auto_increment,
    route_id    int unsigned     default null            comment '单航线查询时填；多机场城市查询或空结果时为 null',
    dep_city    varchar(8)       not null default ''     comment '查询入参：出发城市三字码，如 CSX/BJS',
    arr_city    varchar(8)       not null default ''     comment '查询入参：到达城市三字码，如 BJS',
    dep_date    date             default null            comment '查询的航班日期',
    task_type   varchar(20)      not null default 'search' comment 'search / dynamic / price',
    status      varchar(20)      not null default 'pending' comment 'pending/running/success/failed',
    raw_items   int unsigned     default null            comment '响应中的原始航班条数',
    parsed_rows int unsigned     default null            comment '成功解析入库的舱位行数',
    retry_count tinyint unsigned not null default 0      comment '已重试次数',
    error_msg   text                                     comment '失败原因',
    started_at  datetime         default null            comment '开始时间',
    finished_at datetime         default null            comment '结束时间',
    created_at  datetime         not null default current_timestamp comment '入队时间，用于计算排队时长',
    primary key (id),
    key idx_status (status, started_at),
    key idx_query (dep_city, arr_city, dep_date),
    key idx_route (route_id, started_at),
    constraint fk_task_route foreign key (route_id) references route (id)
) engine = innodb default charset = utf8mb4 collate = utf8mb4_general_ci comment = '采集任务（一次查询请求）';

-- ============================================================
-- 视图：城市对聚合
-- mysql 的 view 不支持 if not exists，用 create or replace 保证幂等
-- ============================================================
create or replace view v_city_route as
select ad.city_code          as dep_city,
       aa.city_code          as arr_city,
       count(*)              as airport_pairs,
       min(r.fetch_interval) as fastest_interval
from route r
         join airport ad on ad.iata = r.dep_iata
         join airport aa on aa.iata = r.arr_iata
where r.status = 'active'
group by ad.city_code, aa.city_code;
