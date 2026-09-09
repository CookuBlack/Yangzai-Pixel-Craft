import video_io
JD=r'..\data\jobs\ee0a091130fa'
AUD=r'..\data\jobs\ee0a091130fa\audio.m4a'
# regenerate user's output with best (playable) to be safe
video_io.rebuild_video(JD+r"\frames", AUD, r'..\data\output\ee0a091130fa.mp4', 24.0, 'best')
print("best regenerated")
# verify lossless profile now playable
video_io.rebuild_video(JD+r"\frames", AUD, r'..\data\_chk_loss.mp4', 24.0, 'lossless')
print("lossless rebuilt")
