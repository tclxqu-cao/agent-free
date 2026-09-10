from job_agent.distance import DISTRICTS, Geocoder, compute_distance, haversine_km
from job_agent.models import Job


def test_haversine_suzhou_shanghai():
    d = haversine_km(31.30, 120.62, 31.23, 121.47)
    assert 70 < d < 95  # 苏州→上海 ≈ 81km


def test_haversine_zero():
    assert haversine_km(31.3, 120.6, 31.3, 120.6) == 0


def test_local_geocode_district(db):
    geo = Geocoder(db, amap_key="", home_city="苏州")
    # 断网路径：amap/nominatim 无 key/失败 → 内置区表
    geo._amap = lambda a, c: None
    geo._nominatim = lambda a, c: None
    coord = geo.geocode("苏州工业园区智选路10号", "苏州")
    assert coord is not None
    assert abs(coord[0] - 31.30) < 0.1  # 工业园区中心


def test_local_geocode_city_center(db):
    geo = Geocoder(db, home_city="苏州")
    coord = geo._local("苏州", "苏州")
    assert coord == tuple(DISTRICTS["苏州"]["__city__"])


def test_geocode_cached(db):
    geo = Geocoder(db, home_city="苏州")
    db.geocode_put("苏州|某地址", 31.0, 120.0)
    assert geo.geocode("某地址", "苏州") == (31.0, 120.0)


def test_compute_distance_address(db, config):
    geo = Geocoder(db, home_city="苏州")
    home = config["home"]
    job = Job(site="boss", title="Java", company="X",
              url="https://x/1", address="苏州工业园区智选路10号",
              city="苏州", district="工业园区")
    d = compute_distance(job, geo, home)
    assert d is not None and d >= 0
    assert job.raw["distance_km"] == d
    # 二次调用走 raw 缓存
    assert compute_distance(job, geo, home) == d


def test_compute_distance_no_home_coords(db, config):
    geo = Geocoder(db, home_city="苏州")
    job = Job(site="boss", title="Java", company="X", url="https://x/2")
    assert compute_distance(job, geo, {"city": "苏州"}) is None
